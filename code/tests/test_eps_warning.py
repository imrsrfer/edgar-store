"""screen.py warns (stderr only) when eps.csv exists but --eps-csv was not passed."""

import screen


def _run(tmp_path, capsys, eps_csv=None, no_eps_ok=False, in_cwd=False, in_root=False):
    root, cwd = tmp_path / "root", tmp_path / "cwd"
    root.mkdir(), cwd.mkdir()
    if in_root:
        (root / "eps.csv").write_text("ticker,fwd_pe,trail_pe\n")
    if in_cwd:
        (cwd / "eps.csv").write_text("ticker,fwd_pe,trail_pe\n")
    found = screen.warn_unused_eps(eps_csv, root, no_eps_ok, cwd=cwd)
    captured = capsys.readouterr()
    return found, captured


def test_warns_on_stderr_when_eps_in_root(tmp_path, capsys):
    found, cap = _run(tmp_path, capsys, in_root=True)
    assert found and found.endswith("eps.csv")
    assert "eps.csv" in cap.err and "adjusted_eps_guard" in cap.err and "BLANK" in cap.err
    assert cap.out == ""


def test_warns_when_eps_in_cwd(tmp_path, capsys):
    found, cap = _run(tmp_path, capsys, in_cwd=True)
    assert found and "adjusted_eps_guard" in cap.err


def test_no_warning_when_eps_csv_passed(tmp_path, capsys):
    found, cap = _run(tmp_path, capsys, eps_csv="eps.csv", in_root=True)
    assert found is None and cap.err == ""


def test_no_warning_when_no_eps_file(tmp_path, capsys):
    found, cap = _run(tmp_path, capsys)
    assert found is None and cap.err == ""


def test_no_eps_ok_silences(tmp_path, capsys):
    found, cap = _run(tmp_path, capsys, no_eps_ok=True, in_root=True)
    assert found is None and cap.err == ""


def test_main_warns_but_exit_code_and_output_unchanged(tmp_path, capsys):
    import shutil
    from pathlib import Path
    store = Path(__file__).resolve().parents[2] / "store"
    root = tmp_path / "store"
    root.mkdir()
    shutil.copy(store / "gate0.csv", root / "gate0.csv")
    (root / "eps.csv").write_text("ticker,fwd_pe,trail_pe\n")
    out = tmp_path / "o.csv"
    assert screen.main(["--lane", "value", "--root", str(root), "--out", str(out)]) == 0
    assert "adjusted_eps_guard" in capsys.readouterr().err
    out2 = tmp_path / "o2.csv"
    assert screen.main(["--lane", "value", "--root", str(root), "--out", str(out2), "--no-eps-ok"]) == 0
    assert "adjusted_eps_guard" not in capsys.readouterr().err
    assert out.read_bytes() == out2.read_bytes()
