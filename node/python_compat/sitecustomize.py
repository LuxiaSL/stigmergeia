"""Lazy, opt-in torch CPU IPC compatibility for the swarm jail.

Loaded through the jail's PYTHONPATH in both main and spawned interpreters.
This is a compatibility adapter, not an isolation mechanism.
"""
import os

if os.environ.get('SWARM_JAILED') == '1' and os.environ.get('SWARM_TORCH_IPC') == 'copy':
    from swarm_torch_ipc import install
    install()
