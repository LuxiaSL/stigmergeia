"""Bounded functional checks; run the driver OUTSIDE the jail on the node.

python3 workload_check.py --venv /path/to/venv --cores 100-103 --json results.json

Each check runs in its own temporary workspace and capped systemd unit.
Defaults to --torch-ipc copy; use --torch-ipc default to compare stock behavior.
CPU only. Validated with PyTorch 2.11 on Python 3.12.
"""
import os, subprocess, sys, time, signal

def own_child_kill():
    p = subprocess.Popen(["sleep", "30"]); p.terminate(); assert p.wait(5) == -signal.SIGTERM
def subprocess_timeout():
    try:
        subprocess.run(["sleep", "30"], timeout=0.5)
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("timeout did not fire")
def mp_pool():
    import multiprocessing as mp
    with mp.get_context("fork").Pool(4) as pool:
        assert sum(pool.map(abs, range(-100, 100))) == 10000
def mp_pool_terminate():
    import multiprocessing as mp
    pool = mp.get_context("fork").Pool(2); pool.map_async(time.sleep, [30, 30]); time.sleep(0.3); pool.terminate(); pool.join()
def mp_spawn_pool():
    import multiprocessing as mp
    with mp.get_context("spawn").Pool(2) as pool:
        assert pool.map(abs, [-1, -2, 3]) == [1, 2, 3]
def dataloader_workers():
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    ds = TensorDataset(torch.arange(256, dtype=torch.float32).reshape(64, 4))
    total = sum(b[0].sum().item() for b in DataLoader(ds, batch_size=8, num_workers=2, timeout=15))
    assert total == sum(range(256)), total
def real_devshm_untouched():
    try:
        os.listdir("/dev/shm")
    except PermissionError:
        return
    raise AssertionError("/dev/shm is listable from inside the jail")
def numpy_matmul():
    import numpy as np
    a = np.random.rand(1500, 1500); t = time.monotonic(); (a @ a).sum()
def threads():
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(8) as ex: assert sum(ex.map(lambda x: x*x, range(100))) == 328350
def workspace_io():
    open("data.bin", "wb").write(os.urandom(10 << 20)); assert os.path.getsize("data.bin") == 10 << 20; os.remove("data.bin")
def torch_train():
    import torch
    torch.manual_seed(0)
    m = torch.nn.Sequential(torch.nn.Linear(64, 128), torch.nn.ReLU(), torch.nn.Linear(128, 1))
    opt = torch.optim.Adam(m.parameters(), 1e-2); x = torch.randn(512, 64); y = x[:, :1] * 2
    for _ in range(200):
        opt.zero_grad(); loss = ((m(x) - y) ** 2).mean(); loss.backward(); opt.step()
    assert loss.item() < 0.5, loss.item()


def dataloader_spawn():
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    ds = TensorDataset(torch.arange(256, dtype=torch.float32).reshape(64, 4))
    loader = DataLoader(ds, batch_size=8, num_workers=2, timeout=15,
                        multiprocessing_context="spawn", persistent_workers=True)
    for _ in range(2):
        assert sum(batch[0].sum().item() for batch in loader) == sum(range(256))


def tensor_roundtrip():
    import torch
    from multiprocessing.reduction import ForkingPickler
    import pickle
    from swarm_torch_ipc import reduce_tensor
    import torch.multiprocessing.reductions as reductions
    assert reductions.reduce_tensor is reduce_tensor
    for original in [torch.arange(24).reshape(4, 6).t(),
                     torch.empty(0), torch.tensor([1, 2], dtype=torch.bfloat16),
                     torch.tensor([1+2j]), torch.tensor([True, False]),
                     torch.tensor([1.0], requires_grad=True),
                     torch.nn.Parameter(torch.ones(2))]:
        copied = pickle.loads(ForkingPickler.dumps(original))
        assert type(copied) is type(original)
        assert torch.equal(copied, original)
        assert copied.dtype == original.dtype and copied.stride() == original.stride()
        assert copied.requires_grad == original.requires_grad
        if original.numel():
            with torch.no_grad():
                copied.fill_(0)
            assert not torch.equal(copied, original)
    try:
        ForkingPickler.dumps(torch.ones(2, requires_grad=True) * 2)
    except RuntimeError:
        pass
    else:
        raise AssertionError("non-leaf autograd tensor was accepted")


CASES = {
    "own-child-terminate": own_child_kill,
    "subprocess-timeout": subprocess_timeout,
    "multiprocessing-pool": mp_pool,
    "pool-terminate": mp_pool_terminate,
    "spawn-pool": mp_spawn_pool,
    "dataloader-2-workers": dataloader_workers,
    "dataloader-spawn-persistent": dataloader_spawn,
    "tensor-roundtrip": tensor_roundtrip,
    "real-devshm-closed": real_devshm_untouched,
    "numpy-matmul": numpy_matmul,
    "threadpool": threads,
    "workspace-io-10MB": workspace_io,
    "torch-cpu-train": torch_train,
}


def main():
    # The driver stays outside the jail. Each case gets its own capped unit:
    # a failed worker or a hung pool cannot hide other results or the table.
    import argparse
    import json
    from pathlib import Path
    import shutil
    import tempfile
    import uuid
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", choices=CASES)
    ap.add_argument("--venv", type=Path)
    ap.add_argument("--cores", default=None)
    ap.add_argument("--torch-ipc", choices=("default", "copy"), default="copy")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    if args.case:
        print("START " + args.case, flush=True)
        CASES[args.case]()
        print("PASS " + args.case, flush=True)
        return 0
    if args.venv is None:
        ap.error("--venv is required for the host driver")
    here = Path(__file__).resolve().parent
    results = []
    for name in CASES:
        with tempfile.TemporaryDirectory(prefix="workload-", dir=here) as tmp:
            ws = Path(tmp)
            shutil.copy2(__file__, ws / "workload_check.py")
            unit = "jail-workload-" + uuid.uuid4().hex
            cmd = [sys.executable, str(here / "jail.py"), "--ws", str(ws),
                   "--venv", str(args.venv.absolute()), "--mem", "4G", "--tasks", "256",
                   "--timeout", "60", "--unit", unit, "--torch-ipc", args.torch_ipc]
            if args.cores:
                cmd += ["--cores", args.cores]
            cmd += ["--", "python", "-u", "workload_check.py", "--case", name]
            started = time.monotonic()
            with tempfile.TemporaryFile(mode="w+") as log:
                process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                problem = None
                try:
                    process.wait(timeout=70)
                except subprocess.TimeoutExpired:
                    problem = "host deadline exceeded"
                finally:
                    try:
                        subprocess.run(["systemctl", "--user", "stop", unit],
                                       capture_output=True, timeout=5)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        problem = f"unit cleanup failed: {exc}"
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait()
                log.seek(0)
                output = log.read()
            ok = problem is None and process.returncode == 0 and ("PASS " + name) in output.splitlines()
            result = {"name": name, "passed": ok, "returncode": process.returncode,
                      "seconds": round(time.monotonic()-started, 2), "error": problem,
                      "output": output}
            results.append(result)
            print(f"{'PASS' if ok else 'FAIL'} {name}: {result['seconds']}s", flush=True)
            if not ok:
                print(problem or output[-4000:], flush=True)
    print("\nWorkload results:", flush=True)
    for result in results:
        print(f"{result['name']:30s} {'PASS' if result['passed'] else 'FAIL'}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n")
    return int(any(not result["passed"] for result in results))


if __name__ == "__main__":
    sys.exit(main())
