"""Send CPU tensors as serialized bytes through multiprocessing pipes.

Opt in with jail.py --torch-ipc copy. Normal PyTorch tensor reducers pass
shared storage using sockets; both upstream sharing strategies need sockets.
This adapter keeps socket() blocked. It supports plain CPU Tensor/Parameter
objects and preserves their serialized dtype, shape, stride and requires_grad.
Each transmitted tensor is an independent copy: storage aliasing across
messages/tensors and shared parameter updates are NOT supported. It costs
serialization time and extra memory. CUDA IPC, custom tensor subclasses,
and explicit storage sharing are outside this mode's contract.

The import hook is lazy (ordinary Python processes don't import torch), and
is installed again by sitecustomize for multiprocessing's spawn children.
Python -I/-S or an application overriding PYTHONPATH bypasses the adapter;
the kernel sandbox still applies.
"""
import importlib.abc
import importlib.machinery
import io
import sys


def rebuild_tensor(payload):
    import torch
    return torch.load(io.BytesIO(payload), map_location='cpu', weights_only=True)


def reduce_tensor(tensor):
    import torch
    if type(tensor) not in (torch.Tensor, torch.nn.Parameter):
        raise TypeError('jail copy IPC supports plain Tensor and Parameter only')
    if tensor.device.type != 'cpu':
        raise RuntimeError('jail copy IPC supports CPU tensors only')
    if tensor.requires_grad and not tensor.is_leaf:
        raise RuntimeError('cannot send a non-leaf tensor requiring gradients; detach it first')
    if tensor.layout != torch.strided or tensor.is_quantized or tensor.is_nested:
        raise TypeError('jail copy IPC supports dense, strided, non-quantized tensors only')
    buffer = io.BytesIO()
    # torch.save uses its own Pickler, not multiprocessing's socket reducers.
    torch.save(tensor, buffer)
    return rebuild_tensor, (buffer.getvalue(),)


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def create_module(self, spec):
        return self.wrapped.create_module(spec)

    def exec_module(self, module):
        self.wrapped.exec_module(module)
        # torch.multiprocessing calls init_reductions after this import returns.
        # Replacing its global also preserves the adapter on re-registration.
        module.reduce_tensor = reduce_tensor


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'torch.multiprocessing.reductions':
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _Loader(spec.loader)
        return spec


def install():
    if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
    module = sys.modules.get('torch.multiprocessing.reductions')
    if module is not None:
        module.reduce_tensor = reduce_tensor
        module.init_reductions()
