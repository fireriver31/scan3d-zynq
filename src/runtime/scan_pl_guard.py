"""Serialize complete old-PYNQ PL metadata transactions in this process.

Do not lock client_request/server_update independently: they share _remote.
Wrap the enclosing query/property instead, keeping all checks and exceptions.
No system-package edits, server restarts, bit downloads or MMIO in install().
Python 3.6. Official reference: Xilinx/PYNQ v2.4 pl.py and overlay.py.
"""
import functools
import importlib
import json
from pathlib import Path
import threading
import types

VERSION = 106
_MARKER = '_scan_rpc_transaction_guard_v106'


def _paired(function):
    code = getattr(function,'__code__',None)
    return code is not None and {'client_request','server_update'}.issubset(code.co_names)


def _serialized(function, state):
    @functools.wraps(function)
    def call(*args, **kwargs):
        with state['lock']:
            try:
                return function(*args,**kwargs)
            except BaseException as error:
                state['last_error'] = '%s: %s'%(type(error).__name__,error)
                raise
    return call


def _wrap_descriptor(value, state):
    if isinstance(value,property):
        functions = [value.fget,value.fset,value.fdel]
        if not any(_paired(f) for f in functions):
            return None
        functions = [_serialized(f,state) if _paired(f) else f for f in functions]
        return property(functions[0],functions[1],functions[2],value.__doc__)
    if isinstance(value,(classmethod,staticmethod)):
        function = value.__func__
        return type(value)(_serialized(function,state)) if _paired(function) else None
    if isinstance(value,types.FunctionType) and _paired(value):
        return _serialized(value,state)
    return None


def install(pl=None, pl_module=None, overlay_module=None):
    """One shared reentrant lock survives module reloads and nested queries."""
    if pl is None:
        from pynq import PL
        pl = PL
    if pl_module is None:
        pl_module = importlib.import_module('pynq.pl')
    if overlay_module is None:
        overlay_module = importlib.import_module('pynq.overlay')
    meta = type(pl)
    existing = vars(meta).get(_MARKER)
    if existing is not None:
        return _info(existing)
    legacy = any('_remote' in vars(base) for base in meta.__mro__)
    state = dict(lock=threading.RLock(),version=VERSION,legacy=legacy,patched=[],last_error=None)
    if legacy:
        owners, seen = [], set()
        for base in meta.__mro__:
            if base is not type and base is not object:
                owners.append(base)
        for module in (pl_module,overlay_module):
            for value in vars(module).values():
                if isinstance(value,type) and value.__module__==module.__name__:
                    owners.append(value)
        changes = []
        for owner in owners:
            if id(owner) in seen:
                continue
            seen.add(id(owner))
            for name,value in list(vars(owner).items()):
                replacement = _wrap_descriptor(value,state)
                if replacement is not None:
                    changes.append((owner,name,value,replacement))
        if not changes:
            raise RuntimeError('发现旧PYNQ共享连接，但未识别完整通信事务；未启动触屏线程')
        applied = []
        try:
            for owner,name,original,replacement in changes:
                setattr(owner,name,replacement)
                applied.append((owner,name,original))
                state['patched'].append(owner.__module__+'.'+owner.__name__+'.'+name)
        except BaseException:
            for owner,name,original in reversed(applied):
                setattr(owner,name,original)
            raise
    setattr(meta,_MARKER,state)
    return _info(state)


def _info(state):
    return dict(version=VERSION,legacy_shared_connection=state['legacy'],
                patched_transactions=list(state['patched']),last_error=state['last_error'],
                overlay_checks_preserved=True,server_restarted=False,bit_downloaded=False)


def healthcheck(pl=None):
    """Read metadata once before any new Overlay download; never fake success."""
    if pl is None:
        from pynq import PL
        pl = PL
    state = vars(type(pl)).get(_MARKER)
    if state is None:
        raise RuntimeError('尚未安装PL通信串行保护')
    try:
        with state['lock']:
            name = pl.bitfile_name
    except Exception as error:
        raise RuntimeError('PL元数据服务仍不可用，尚未重新加载bit。请先恢复板上PL服务；'
                           '反复运行扫描入口不会恢复它。原错误：%s: %s'%
                           (type(error).__name__,error)) from error
    return dict(_info(state),healthcheck_passed=True,reported_bitfile=str(name))


def save(directory, record):
    if directory is None:
        return
    try:
        path = Path(directory)/'1030_pl_rpc_guard_v106.json'
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
    except OSError as error:
        print('PL通信保护记录暂未保存：%s'%error,flush=True)
