"""Execute official old-PYNQ transaction bodies with controlled channels.

The BSD-licensed sources stay under sim/reference unchanged.
Only the server transport is simulated; no actual PYNQ, FPGA or sensor runs.
"""
from pathlib import Path
import ast
import copy
from datetime import datetime
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = Path(os.environ.get('SCAN3D_TEST_OUTPUT', str(ROOT/'sim/out')))/'rpc'
WORK.mkdir(parents=True,exist_ok=True)
SOURCE = ROOT/'sim/reference'
spec = importlib.util.spec_from_file_location('guard_under_test',ROOT/'src/runtime/scan_pl_guard.py')
guard = importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class Backend:
    def __init__(self, race=False):
        self.race = race
        self.lock = threading.Lock()
        self.active = self.max_active = self.requests = 0
        self.channels = []
        self.b_receiving = threading.Event()
        self.b_closed = threading.Event()
        self.fail_connect = False
        self.fail_receive = False

    def client(self,*args,**kwargs):
        if self.fail_connect:
            raise FileNotFoundError('simulated missing PL socket')
        with self.lock:
            self.requests += 1
            self.active += 1
            self.max_active = max(self.max_active,self.active)
            channel = Channel(self,threading.current_thread().name)
            self.channels.append(channel)
            return channel

    def cleanup(self):
        for channel in self.channels:
            channel.close()


class Channel:
    def __init__(self,backend,name):
        self.backend,self.name = backend,name
        self.closed = False
        self.handle = os.open(os.devnull,os.O_RDONLY)

    def recv(self):
        if self.backend.fail_receive:
            self.backend.fail_receive = False
            raise EOFError('simulated receive failure')
        if self.backend.race and self.name=='A':
            if not self.backend.b_receiving.wait(2):raise RuntimeError('schedule A timed out')
        elif self.backend.race and self.name=='B':
            self.backend.b_receiving.set()
            if not self.backend.b_closed.wait(2):raise RuntimeError('schedule B timed out')
            # An actual OS read on the prematurely closed handle, not a
            # hard-coded TypeError. Official client_request calls this recv.
            os.read(self.handle,0)
        else:
            time.sleep(.0001)
        return ['/fixture.bit','stamp',{'camera_control':{'type':'fixture'}},{},{},{},{}]

    def send(self,value):
        if self.closed:os.read(self.handle,0)
        assert len(value)==8

    def close(self):
        if not self.closed:
            os.close(self.handle)
            self.handle = None
            self.closed = True
            with self.backend.lock:self.backend.active -= 1
            if self.name=='B':self.backend.b_closed.set()


def legacy(backend):
    pl_path = SOURCE/'pynq_v24_pl.py'
    overlay_path = SOURCE/'pynq_v24_overlay.py'
    pl_tree = ast.parse(pl_path.read_text(encoding='utf8'))
    overlay_tree = ast.parse(overlay_path.read_text(encoding='utf8'))
    meta = next(n for n in pl_tree.body if isinstance(n,ast.ClassDef) and n.name=='PLMeta')
    functions = [copy.deepcopy(n) for n in meta.body if isinstance(n,ast.FunctionDef)]
    extracted = ast.ClassDef(name='PLMeta',bases=[ast.Name(id='type',ctx=ast.Load())],
                            keywords=[],body=functions,decorator_list=[])
    namespace = dict(__name__='pynq.pl',Client=backend.client,PL_SERVER_FILE='fixture',datetime=datetime)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[extracted],type_ignores=[])),str(pl_path),'exec'),namespace)
    exec('class PL(metaclass=PLMeta):\n    pass\n',namespace)
    pl = namespace['PL']
    setattr(type(pl),'_remote',None)
    for name,value in dict(_bitfile_name='/fixture.bit',_timestamp='stamp',_ip_dict={},
        _gpio_dict={},_interrupt_controllers={},_interrupt_pins={},_hierarchy_dict={}).items():
        setattr(type(pl),name,value)
    bitstream = next(n for n in pl_tree.body if isinstance(n,ast.ClassDef) and n.name=='Bitstream')
    update = copy.deepcopy(next(n for n in bitstream.body if getattr(n,'name',None)=='update_pl'))
    cls = ast.ClassDef(name='Bitstream',bases=[],keywords=[],body=[update],decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(pl_path),'exec'),namespace)
    pl_module = types.ModuleType('pynq.pl')
    pl_module.PL,pl_module.PLMeta,pl_module.Bitstream = pl,type(pl),namespace['Bitstream']
    overlay = next(n for n in overlay_tree.body if isinstance(n,ast.ClassDef) and n.name=='Overlay')
    method = copy.deepcopy(next(n for n in overlay.body if getattr(n,'name',None)=='is_loaded'))
    cls = ast.ClassDef(name='Overlay',bases=[],keywords=[],body=[method],decorator_list=[])
    overlay_namespace = dict(__name__='pynq.overlay',PL=pl)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls],type_ignores=[])),str(overlay_path),'exec'),overlay_namespace)
    overlay_module = types.ModuleType('pynq.overlay')
    overlay_module.Overlay = overlay_namespace['Overlay']
    instance = overlay_module.Overlay()
    instance.timestamp,instance.bitfile_name = 'stamp','/fixture.bit'
    return pl,pl_module,overlay_module,instance


class Tests(unittest.TestCase):
    def test_original_shared_connection_race_matches_closed_handle_failure(self):
        backend = Backend(race=True)
        pl,pm,om,overlay = legacy(backend)
        errors = []
        def worker():
            try:overlay.is_loaded()
            except BaseException as error:errors.append((threading.current_thread().name,error))
        threads = [threading.Thread(target=worker,name=name) for name in ('A','B')]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        try:
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertTrue(any(name=='B' and isinstance(error,TypeError) for name,error in errors),errors)
            self.assertEqual(backend.max_active,2)
        finally:backend.cleanup()

    def test_guard_serializes_900_official_overlay_property_and_update_transactions(self):
        backend = Backend()
        pl,pm,om,overlay = legacy(backend)
        report = guard.install(pl,pm,om)
        errors = []
        def worker(kind):
            try:
                for i in range(300):
                    if kind==0:self.assertTrue(overlay.is_loaded())
                    elif kind==1:self.assertEqual(pl.timestamp,'stamp')
                    else:self.assertIn('camera_control',pl.ip_dict)
            except BaseException as error:errors.append(error)
        threads = [threading.Thread(target=worker,args=(kind,)) for kind in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        try:
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertFalse(errors,errors)
            self.assertEqual(backend.requests,900)
            self.assertEqual(backend.max_active,1)
            self.assertEqual(backend.active,0)
            self.assertIn('pynq.overlay.Overlay.is_loaded',report['patched_transactions'])
            self.assertIn('pynq.pl.PLMeta.ip_dict',report['patched_transactions'])
            self.assertIn('pynq.pl.Bitstream.update_pl',report['patched_transactions'])
        finally:backend.cleanup()

    def test_install_neither_queries_hardware_nor_wraps_half_transactions(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        before = (vars(type(pl))['client_request'],vars(type(pl))['server_update'])
        guard.install(pl,pm,om)
        self.assertEqual(backend.requests,0)
        self.assertEqual(before,(vars(type(pl))['client_request'],vars(type(pl))['server_update']))

    def test_reload_and_reinstall_keep_one_lock_and_original_checks(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        first = guard.install(pl,pm,om)
        method = om.Overlay.is_loaded
        spec = importlib.util.spec_from_file_location('guard_reload',ROOT/'src/runtime/scan_pl_guard.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(first,module.install(pl,pm,om))
        self.assertIs(method,om.Overlay.is_loaded)
        overlay.timestamp = 'different bit timestamp'
        self.assertFalse(overlay.is_loaded())
        backend.cleanup()

    def test_nested_metadata_call_is_reentrant(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        guard.install(pl,pm,om)
        result = []
        def nested():
            with vars(type(pl))[guard._MARKER]['lock']:
                result.append(overlay.is_loaded())
                result.append(pl.timestamp)
        thread = threading.Thread(target=nested)
        thread.start();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result,[True,'stamp'])
        self.assertEqual(backend.active,0)
        backend.cleanup()

    def test_receive_exception_releases_lock_for_other_thread(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        guard.install(pl,pm,om);backend.fail_receive = True
        with self.assertRaises(EOFError):overlay.is_loaded()
        result = []
        thread = threading.Thread(target=lambda:result.append(overlay.is_loaded()))
        thread.start();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(result,[True])
        backend.cleanup()

    def test_dead_server_healthcheck_fails_before_any_download(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        guard.install(pl,pm,om);backend.fail_connect = True
        with self.assertRaisesRegex(RuntimeError,'尚未重新加载bit') as context:
            guard.healthcheck(pl)
        self.assertIsInstance(context.exception.__cause__,ConnectionError)
        self.assertEqual(backend.requests,0)

    def test_healthy_read_only_healthcheck_preserves_metadata(self):
        backend = Backend();pl,pm,om,overlay = legacy(backend)
        guard.install(pl,pm,om)
        record = guard.healthcheck(pl)
        self.assertTrue(record['healthcheck_passed'])
        self.assertFalse(record['server_restarted'])
        self.assertFalse(record['bit_downloaded'])
        self.assertEqual(record['reported_bitfile'],'/fixture.bit')
        backend.cleanup()


if __name__=='__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    report = dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        official_functions_executed_with_simulated_transport=True,
        unsafe_closed_handle_race_reproduced=result.wasSuccessful(),serialized_transactions=900,
        board_tested=False,sources=[dict(file=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(SOURCE.glob('pynq_v24_*.py'))])
    (WORK/'rpc_checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    sys.exit(0 if result.wasSuccessful() else 1)
