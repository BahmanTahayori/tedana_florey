"""Integration tests for "real" data."""

import glob
import json
import logging
import os
import os.path as op
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from gzip import GzipFile
from io import BytesIO

import pandas as pd
import pytest
import requests
from pkg_resources import resource_filename

from tedana.io import InputHarvester
from tedana.workflows import t2smap as t2smap_cli
from tedana.workflows import tedana as tedana_cli
from tedana.workflows.ica_reclassify import ica_reclassify_workflow

# Need to see if a no BOLD warning occurred
LOGGER = logging.getLogger(__name__)
# Added a testing logger to output whether or not testing data were downlaoded
TestLGR = logging.getLogger("TESTING")


def check_integration_outputs(fname, outpath, n_logs=1):
    """
    Checks outputs of integration tests.

    Parameters
    ----------
    fname : str
        Path to file with expected outputs
    outpath : str
        Path to output directory generated from integration tests
    """

    # Gets filepaths generated by integration test
    found_files = [
        os.path.relpath(f, outpath)
        for f in glob.glob(os.path.join(outpath, "**"), recursive=True)[1:]
    ]

    # Checks for log file
    log_regex = "^tedana_[12][0-9]{3}-[0-9]{2}-[0-9]{2}T[0-9]{2}[0-9]{2}[0-9]{2}.tsv$"
    logfiles = [out for out in found_files if re.match(log_regex, out)]
    assert len(logfiles) == n_logs

    # Removes logfiles from list of existing files
    for log in logfiles:
        found_files.remove(log)

    # Compares remaining files with those expected
    with open(fname) as f:
        expected_files = f.read().splitlines()
    expected_files = [os.path.normpath(path) for path in expected_files]

    if sorted(found_files) != sorted(expected_files):
        expected_not_found = sorted(list(set(expected_files) - set(found_files)))
        found_not_expected = sorted(list(set(found_files) - set(expected_files)))

        msg = ""
        if expected_not_found:
            msg += "\nExpected but not found:\n\t"
            msg += "\n\t".join(expected_not_found)

        if found_not_expected:
            msg += "\nFound but not expected:\n\t"
            msg += "\n\t".join(found_not_expected)
        raise ValueError(msg)


def data_for_testing_info(test_dataset=str):
    """
    Get the path and download link for each dataset used for testing.

    Also creates the base directories into which the data and output
    directories are written

    Parameters
    ----------
    test_dataset : str
       References one of the datasets to download. It can be:
        three-echo
        three-echo-reclassify
        four-echo
        five-echo

    Returns
    -------
    test_data_path : str
       The path to the local directory where the data will be downloaded
    osf_id : str
       The ID for the OSF file.
       Data download link would be https://osf.io/osf_id/download
       Metadata download link would be https://osf.io/osf_id/metadata/?format=datacite-json
    """

    tedana_path = os.path.dirname(tedana_cli.__file__)
    base_data_path = os.path.abspath(os.path.join(tedana_path, "../../.testing_data_cache"))
    os.makedirs(base_data_path, exist_ok=True)
    os.makedirs(os.path.join(base_data_path, "outputs"), exist_ok=True)
    if test_dataset == "three-echo":
        test_data_path = os.path.join(base_data_path, "three-echo/TED.three-echo")
        osf_id = "rqhfc"
        os.makedirs(os.path.join(base_data_path, "three-echo"), exist_ok=True)
        os.makedirs(os.path.join(base_data_path, "outputs/three-echo"), exist_ok=True)
    elif test_dataset == "three-echo-reclassify":
        test_data_path = os.path.join(base_data_path, "reclassify")
        osf_id = "f6g45"
        os.makedirs(os.path.join(base_data_path, "outputs/reclassify"), exist_ok=True)
    elif test_dataset == "four-echo":
        test_data_path = os.path.join(base_data_path, "four-echo/TED.four-echo")
        osf_id = "gnj73"
        os.makedirs(os.path.join(base_data_path, "four-echo"), exist_ok=True)
        os.makedirs(os.path.join(base_data_path, "outputs/four-echo"), exist_ok=True)
    elif test_dataset == "five-echo":
        test_data_path = os.path.join(base_data_path, "five-echo/TED.five-echo")
        osf_id = "9c42e"
        os.makedirs(os.path.join(base_data_path, "five-echo"), exist_ok=True)
        os.makedirs(os.path.join(base_data_path, "outputs/five-echo"), exist_ok=True)
    else:
        raise ValueError(f"{test_dataset} is not a valid dataset string for data_for_testing_info")

    return test_data_path, osf_id


def download_test_data(osf_id, test_data_path):
    """
    If current data is not already available, downloads tar.gz data
    stored at `https://osf.io/osf_id/download`.

    and unpacks into `out_path`.

    Parameters
    ----------
    osf_id : str
       The ID for the OSF file.
    out_path : str
        Path to directory where OSF data should be extracted
    """

    try:
        datainfo = requests.get(f"https://osf.io/{osf_id}/metadata/?format=datacite-json")
    except Exception:
        if len(os.listdir(test_data_path)) == 0:
            raise ConnectionError(
                f"Cannot access https://osf.io/{osf_id} and testing data " "are not yet downloaded"
            )
        else:
            TestLGR.warning(
                f"Cannot access https://osf.io/{osf_id}. "
                f"Using local copy of testing data in {test_data_path} "
                "but cannot validate that local copy is up-to-date"
            )
            return
    datainfo.raise_for_status()
    metadata = json.loads(datainfo.content)
    # 'dates' is a list with all udpates to the file, the last item in the list
    # is the most recent and the 'date' field in the list is the date of the last
    # update.
    osf_filedate = metadata["dates"][-1]["date"]

    # File the file with the most recent date for comparision with
    # the lsst updated date for the osf file
    if os.path.exists(test_data_path):
        filelist = glob.glob(f"{test_data_path}/*")
        most_recent_file = max(filelist, key=os.path.getctime)
        if os.path.exists(most_recent_file):
            local_filedate = os.path.getmtime(most_recent_file)
            local_filedate_str = str(datetime.fromtimestamp(local_filedate).date())
            local_data_exists = True
        else:
            local_data_exists = False
    else:
        local_data_exists = False
    if local_data_exists:
        if local_filedate_str == osf_filedate:
            TestLGR.info(
                f"Downloaded and up-to-date data already in {test_data_path}. Not redownloading"
            )
            return
        else:
            TestLGR.info(
                f"Downloaded data in {test_data_path} was last modified on "
                f"{local_filedate_str}. Data on https://osf.io/{osf_id} "
                f" was last updated on {osf_filedate}. Deleting and redownloading"
            )
            shutil.rmtree(test_data_path)
    req = requests.get(f"https://osf.io/{osf_id}/download")
    req.raise_for_status()
    t = tarfile.open(fileobj=GzipFile(fileobj=BytesIO(req.content)))
    os.makedirs(test_data_path, exist_ok=True)
    t.extractall(test_data_path)


def reclassify_raw() -> str:
    test_data_path, _ = data_for_testing_info("three-echo-reclassify")
    return os.path.join(test_data_path, "TED.three-echo")


def reclassify_raw_registry() -> str:
    return os.path.join(reclassify_raw(), "desc-tedana_registry.json")


def guarantee_reclassify_data() -> None:
    """Ensures that the reclassify data exists at the expected path and return path."""

    test_data_path, osf_id = data_for_testing_info("three-echo-reclassify")

    # Should now be checking and not downloading for each test so don't see if statement here
    # if not os.path.exists(reclassify_raw_registry()):
    download_test_data(osf_id, test_data_path)
    # else:
    # Path exists, be sure that everything in registry exists
    ioh = InputHarvester(reclassify_raw_registry())
    all_present = True
    for _, v in ioh.registry.items():
        if not isinstance(v, list):
            if not os.path.exists(os.path.join(reclassify_raw(), v)):
                all_present = False
                break
    if not all_present:
        # Something was removed, need to re-download
        shutil.rmtree(reclassify_raw())
        guarantee_reclassify_data()
    return test_data_path


def test_integration_five_echo(skip_integration):
    """Integration test of the full tedana workflow using five-echo test data."""

    if skip_integration:
        pytest.skip("Skipping five-echo integration test")

    test_data_path, osf_id = data_for_testing_info("five-echo")
    out_dir = os.path.abspath(os.path.join(test_data_path, "../../outputs/five-echo"))
    # out_dir_manual = f"{out_dir}-manual"

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    # if os.path.exists(out_dir_manual):
    #     shutil.rmtree(out_dir_manual)

    # download data and run the test
    download_test_data(osf_id, test_data_path)
    prepend = f"{test_data_path}/p06.SBJ01_S09_Task11_e"
    suffix = ".sm.nii.gz"
    datalist = [prepend + str(i + 1) + suffix for i in range(5)]
    echo_times = [15.4, 29.7, 44.0, 58.3, 72.6]
    tedana_cli.tedana_workflow(
        data=datalist,
        tes=echo_times,
        out_dir=out_dir,
        tedpca=0.95,
        fittype="curvefit",
        fixed_seed=49,
        tedort=True,
        verbose=True,
        prefix="sub-01",
    )

    # Just a check on the component table pending a unit test of load_comptable
    comptable = os.path.join(out_dir, "sub-01_desc-tedana_metrics.tsv")
    df = pd.read_table(comptable)
    assert isinstance(df, pd.DataFrame)

    # compare the generated output files
    fn = resource_filename("tedana", "tests/data/nih_five_echo_outputs_verbose.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_four_echo(skip_integration):
    """Integration test of the full tedana workflow using four-echo test data."""

    if skip_integration:
        pytest.skip("Skipping four-echo integration test")

    test_data_path, osf_id = data_for_testing_info("four-echo")
    out_dir = os.path.abspath(os.path.join(test_data_path, "../../outputs/four-echo"))
    out_dir_manual = f"{out_dir}-manual"

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    if os.path.exists(out_dir_manual):
        shutil.rmtree(out_dir_manual)

    # download data and run the test
    download_test_data(osf_id, test_data_path)
    prepend = f"{test_data_path}/sub-PILOT_ses-01_task-localizerDetection_run-01_echo-"
    suffix = "_space-sbref_desc-preproc_bold+orig.HEAD"
    datalist = [prepend + str(i + 1) + suffix for i in range(4)]
    tedana_cli.tedana_workflow(
        data=datalist,
        mixm=op.join(op.dirname(datalist[0]), "desc-ICA_mixing_static.tsv"),
        tes=[11.8, 28.04, 44.28, 60.52],
        out_dir=out_dir,
        tedpca="kundu-stabilize",
        gscontrol=["gsr", "mir"],
        png_cmap="bone",
        prefix="sub-01",
        debug=True,
        verbose=True,
    )

    ica_reclassify_workflow(
        op.join(out_dir, "sub-01_desc-tedana_registry.json"),
        accept=[1, 2, 3],
        reject=[4, 5, 6],
        out_dir=out_dir_manual,
        mir=True,
    )

    # compare the generated output files
    fn = resource_filename("tedana", "tests/data/fiu_four_echo_outputs.txt")

    check_integration_outputs(fn, out_dir)


def test_integration_three_echo(skip_integration):
    """Integration test of the full tedana workflow using three-echo test data."""

    if skip_integration:
        pytest.skip("Skipping three-echo integration test")

    test_data_path, osf_id = data_for_testing_info("three-echo")
    out_dir = os.path.abspath(os.path.join(test_data_path, "../../outputs/three-echo"))
    out_dir_manual = f"{out_dir}-rerun"

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    if os.path.exists(out_dir_manual):
        shutil.rmtree(out_dir_manual)

    # download data and run the test
    download_test_data(osf_id, test_data_path)
    tedana_cli.tedana_workflow(
        data=f"{test_data_path}/three_echo_Cornell_zcat.nii.gz",
        tes=[14.5, 38.5, 62.5],
        out_dir=out_dir,
        low_mem=True,
        tedpca="aic",
    )

    # Test re-running, but use the CLI
    args = [
        "-d",
        f"{test_data_path}/three_echo_Cornell_zcat.nii.gz",
        "-e",
        "14.5",
        "38.5",
        "62.5",
        "--out-dir",
        out_dir_manual,
        "--debug",
        "--verbose",
        "-f",
        "--mix",
        os.path.join(out_dir, "desc-ICA_mixing.tsv"),
    ]
    tedana_cli._main(args)

    # compare the generated output files
    fn = resource_filename("tedana", "tests/data/cornell_three_echo_outputs.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_insufficient_args(skip_integration):
    if skip_integration:
        pytest.skip("Skipping reclassify insufficient args")

    guarantee_reclassify_data()

    args = [
        "ica_reclassify",
        reclassify_raw_registry(),
    ]

    result = subprocess.run(args, capture_output=True)
    assert b"ValueError: Must manually accept or reject" in result.stderr
    assert result.returncode != 0


def test_integration_reclassify_quiet_csv(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify quiet csv")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/quiet"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    # Make some files that have components to manually accept and reject
    to_accept = [i for i in range(3)]
    to_reject = [i for i in range(7, 4)]
    acc_df = pd.DataFrame(data=to_accept, columns=["Components"])
    rej_df = pd.DataFrame(data=to_reject, columns=["Components"])
    acc_csv_fname = os.path.join(reclassify_raw(), "accept.csv")
    rej_csv_fname = os.path.join(reclassify_raw(), "reject.csv")
    acc_df.to_csv(acc_csv_fname)
    rej_df.to_csv(rej_csv_fname)

    args = [
        "ica_reclassify",
        "--manacc",
        acc_csv_fname,
        "--manrej",
        rej_csv_fname,
        "--out-dir",
        out_dir,
        reclassify_raw_registry(),
    ]

    results = subprocess.run(args, capture_output=True)
    assert results.returncode == 0
    fn = resource_filename("tedana", "tests/data/reclassify_quiet_out.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_quiet_spaces(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify quiet space-delimited integers")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/quiet"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    args = [
        "ica_reclassify",
        "--manacc",
        "1",
        "2",
        "3",
        "--manrej",
        "4",
        "5",
        "6",
        "--out-dir",
        out_dir,
        reclassify_raw_registry(),
    ]

    results = subprocess.run(args, capture_output=True)
    assert results.returncode == 0
    fn = resource_filename("tedana", "tests/data/reclassify_quiet_out.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_quiet_string(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify quiet string of integers")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/quiet"))

    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    args = [
        "ica_reclassify",
        "--manacc",
        "1,2,3",
        "--manrej",
        "4,5,6,",
        "--out-dir",
        out_dir,
        reclassify_raw_registry(),
    ]

    results = subprocess.run(args, capture_output=True)
    assert results.returncode == 0
    fn = resource_filename("tedana", "tests/data/reclassify_quiet_out.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_debug(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify debug")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/debug"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    args = [
        "ica_reclassify",
        "--manacc",
        "1",
        "2",
        "3",
        "--prefix",
        "sub-testymctestface",
        "--convention",
        "orig",
        "--tedort",
        "--mir",
        "--no-reports",
        "--out-dir",
        out_dir,
        "--debug",
        reclassify_raw_registry(),
    ]

    results = subprocess.run(args, capture_output=True)
    assert results.returncode == 0
    fn = resource_filename("tedana", "tests/data/reclassify_debug_out.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_both_rej_acc(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify both rejected and accepted")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/both_rej_acc"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    with pytest.raises(
        ValueError,
        match=r"The following components were both accepted and",
    ):
        ica_reclassify_workflow(
            reclassify_raw_registry(),
            accept=[1, 2, 3],
            reject=[1, 2, 3],
            out_dir=out_dir,
        )


def test_integration_reclassify_run_twice(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify both rejected and accepted")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/run_twice"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    ica_reclassify_workflow(
        reclassify_raw_registry(),
        accept=[1, 2, 3],
        out_dir=out_dir,
        no_reports=True,
    )
    ica_reclassify_workflow(
        reclassify_raw_registry(),
        accept=[1, 2, 3],
        out_dir=out_dir,
        overwrite=True,
        no_reports=True,
    )
    fn = resource_filename("tedana", "tests/data/reclassify_run_twice.txt")
    check_integration_outputs(fn, out_dir, n_logs=2)


def test_integration_reclassify_no_bold(skip_integration, caplog):
    if skip_integration:
        pytest.skip("Skip reclassify both rejected and accepted")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/no_bold"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    ioh = InputHarvester(reclassify_raw_registry())
    comptable = ioh.get_file_contents("ICA metrics tsv")
    to_accept = [i for i in range(len(comptable))]

    ica_reclassify_workflow(
        reclassify_raw_registry(),
        reject=to_accept,
        out_dir=out_dir,
        no_reports=True,
    )
    assert "No accepted components remaining after manual classification!" in caplog.text

    fn = resource_filename("tedana", "tests/data/reclassify_no_bold.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_accrej_files(skip_integration, caplog):
    if skip_integration:
        pytest.skip("Skip reclassify both rejected and accepted")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/no_bold"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    ioh = InputHarvester(reclassify_raw_registry())
    comptable = ioh.get_file_contents("ICA metrics tsv")
    to_accept = [i for i in range(len(comptable))]

    ica_reclassify_workflow(
        reclassify_raw_registry(),
        reject=to_accept,
        out_dir=out_dir,
        no_reports=True,
    )
    assert "No accepted components remaining after manual classification!" in caplog.text

    fn = resource_filename("tedana", "tests/data/reclassify_no_bold.txt")
    check_integration_outputs(fn, out_dir)


def test_integration_reclassify_index_failures(skip_integration):
    if skip_integration:
        pytest.skip("Skip reclassify index failures")

    test_data_path = guarantee_reclassify_data()
    out_dir = os.path.abspath(os.path.join(test_data_path, "../outputs/reclassify/index_failures"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    with pytest.raises(
        ValueError,
        match=r"_parse_manual_list expected a list of integers, but the input is",
    ):
        ica_reclassify_workflow(
            reclassify_raw_registry(),
            accept=[1, 2.5, 3],
            out_dir=out_dir,
            no_reports=True,
        )

    with pytest.raises(
        ValueError,
        match=r"_parse_manual_list expected integers or a filename, but the input is",
    ):
        ica_reclassify_workflow(
            reclassify_raw_registry(),
            accept=[2.5],
            out_dir=out_dir,
            no_reports=True,
        )


def test_integration_t2smap(skip_integration):
    """Integration test of the full t2smap workflow using five-echo test data."""
    if skip_integration:
        pytest.skip("Skipping t2smap integration test")
    test_data_path, osf_id = data_for_testing_info("five-echo")
    out_dir = os.path.abspath(os.path.join(test_data_path, "../../outputs/t2smap_five-echo"))
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    # download data and run the test
    download_test_data(osf_id, test_data_path)
    prepend = f"{test_data_path}/p06.SBJ01_S09_Task11_e"
    suffix = ".sm.nii.gz"
    datalist = [prepend + str(i + 1) + suffix for i in range(5)]
    echo_times = [15.4, 29.7, 44.0, 58.3, 72.6]
    args = (
        ["-d"]
        + datalist
        + ["-e"]
        + [str(te) for te in echo_times]
        + ["--out-dir", out_dir, "--fittype", "curvefit"]
    )
    t2smap_cli._main(args)

    # compare the generated output files
    fname = resource_filename("tedana", "tests/data/nih_five_echo_outputs_t2smap.txt")
    # Gets filepaths generated by integration test
    found_files = [
        os.path.relpath(f, out_dir)
        for f in glob.glob(os.path.join(out_dir, "**"), recursive=True)[1:]
    ]

    # Compares remaining files with those expected
    with open(fname) as f:
        expected_files = f.read().splitlines()
    assert sorted(expected_files) == sorted(found_files)
