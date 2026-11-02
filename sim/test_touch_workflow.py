"""Host-only interaction tests. No PYNQ Overlay, MMIO, sensor or scan run."""
from pathlib import Path
from contextlib import contextmanager
import ast
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT/'src'
WORK = Path(os.environ.get('SCAN3D_TEST_OUTPUT', str(ROOT/'sim/out')))/'touch'
RUN = WORK/('checks_'+str(int(time.time()*1000)))
RUN.mkdir(parents=True)
sys.path.insert(0,str(HERE/'runtime'))
BASE = ROOT/'src'
sys.path.insert(1,str(BASE/'runtime'))
sys.path.insert(2,str(ROOT/'src/runtime'))


def load(name,path):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flow = load('flow_under_test',HERE/'runtime/touch_scan.py')
screen = load('screen_under_test',HERE/'runtime/scan_screen.py')
viewer_module = load('viewer_under_test',ROOT/'src/runtime/tft_viewer.py')
sys.modules['tft_viewer'] = viewer_module


class Hardware:
    def __init__(self):
        self.touch = dict(sequence=0,online=True,down=False,points=0,x=0,y=0,
                          product=0x313139,max_x=800,max_y=480,errors=0)
        self.front = None
        self.frames = []
    def snapshot(self):
        return dict(self.touch)
    def event(self,x=0,y=0,down=False,points=None,online=True):
        self.touch.update(sequence=self.touch['sequence']+1,x=x,y=y,down=down,
                          points=(1 if down else 0) if points is None else points,online=online)
    def present(self,value):
        assert value.shape==(480,800) and value.dtype==np.uint16
        self.front = 0
        self.frames.append(threading.get_ident())
    def camera(self):
        self.front = None
    def close(self):
        self.front = None


class Writer:
    def __init__(self):
        self.frames = []
    def flush(self,pixels):
        self.frames.append(threading.get_ident())
        return 0


class Renderer:
    info = {'fixture_only':True}
    def render(self,*args):
        return np.zeros((208,400),np.uint16)


class Session:
    def __init__(self,directory):
        self.viewer_lock = threading.RLock()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.workflow = None
        self.canvas = screen.BitmapCanvas()
        self.writer = Writer()
        self.hardware = Hardware()
        self.viewer = viewer_module.Viewer(None,directory,hardware=self.hardware,renderer_factory=lambda path:Renderer())
        self.viewer.cal_path = directory/'calibration.json'
        self.viewer.matrix = np.array([[1,0],[0,1],[0,0]],float)
        self.viewer.cal_signature = [0x313139,800,480]
        self.state = dict(step=0,status='ready',stage='初始化',frame='',detail='',volumes=[],
                          started_at=time.monotonic(),finished_at=None,model_path=None)
        self.last_render_state = None
    def snapshot(self):
        with self.lock:
            return dict(self.state)


def fixture(name):
    directory = RUN/name
    directory.mkdir()
    session = Session(directory)
    controller = flow.Controller(session,screen).install()
    return directory,session,controller


def tick(session):
    with session.viewer_lock:
        session.viewer.tick(session.canvas,session.writer,session.snapshot(),session.stop)


def tap(session,rect):
    x,y = (rect[0]+rect[2])//2,(rect[1]+rect[3])//2
    session.hardware.event(x,y,True)
    tick(session)
    session.hardware.event(x,y,False)
    tick(session)


class Tests(unittest.TestCase):
    def test_no_session_reports_recorded_attach_cause(self):
        interface = types.SimpleNamespace(_LAST_ATTACH=dict(phase='metadata',error='screen lock busy'))
        with self.assertRaisesRegex(RuntimeError,'screen lock busy'):
            flow.Controller(None,interface)

    def test_real_ui_worker_preserves_first_failure_and_cleanup_failure(self):
        directory = RUN/'worker_failure';directory.mkdir()
        overlay = types.SimpleNamespace(ip_dict={},hierarchy_dict={},bitfile_name='fixture',is_loaded=lambda:True)
        session = screen.ScreenSession(overlay,types.SimpleNamespace(),directory)
        class BadViewer:
            error = None
            def tick(self,*args):raise ValueError('original RPC failure')
            def _save(self):pass
            def close(self):raise ConnectionError('cleanup PL server unavailable')
        session.viewer = BadViewer()
        session.workflow = object()
        session._run()
        self.assertTrue(session.stop.is_set())
        self.assertIn('original RPC failure',session.viewer_failure_traceback)
        self.assertIn('cleanup PL server unavailable',session.thread_failure_traceback)
        self.assertIn('original RPC failure',session.viewer_error)
        controller = flow.Controller(session,screen)
        with self.assertRaisesRegex(RuntimeError,'original RPC failure') as context:
            controller.wait()
        self.assertIn('cleanup PL server unavailable',str(context.exception))
        record = session.snapshot()
        self.assertIn('original RPC failure',record['viewer_failure_traceback'])

    def test_existing_nine_cells_keep_all_algorithm_code(self):
        path = ROOT/'src/runtime/scan_workflow.ipynb'
        cells,digest = flow.load_cells(path)
        old = [''.join(c['source']) for c in json.loads(path.read_text(encoding='utf8'))['cells'] if c['cell_type']=='code']
        self.assertEqual(cells[:8],old[:8])
        before = ast.parse(old[8])
        before.body = [node for node in before.body if not (isinstance(node,ast.Expr)
            and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute)
            and isinstance(node.value.func.value,ast.Name) and node.value.func.value.id=='scan_screen'
            and node.value.func.attr=='detach')]
        self.assertEqual(ast.dump(before),ast.dump(ast.parse(cells[8])))
        self.assertEqual(len(digest),64)

    def test_unsafe_initialization_rejected_before_any_execution(self):
        path = RUN/'unsafe.ipynb'
        cells = [dict(cell_type='code',source='Overlay()')]+[
            dict(cell_type='code',source='with scan_screen.step(%d, globals()):\n    pass\n'%i) for i in range(1,9)]
        path.write_text(json.dumps(dict(cells=cells)),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'DDR'):
            flow.load_cells(path)

    def test_one_tap_enqueues_one_step_and_busy_taps_do_not_queue(self):
        directory,session,controller = fixture('one_tap')
        tick(session)
        tap(session,flow.NEXT)
        command = controller.commands.get_nowait()
        self.assertEqual((command['action'],command['step']),('execute',1))
        tap(session,flow.NEXT)
        self.assertTrue(controller.commands.empty())
        controller.detach()

    def test_ready_after_finished_step_accepts_first_new_tap(self):
        directory,session,controller = fixture('ready_after_review')
        tick(session)
        controller.set_phase('review',3)
        tick(session)  # Same unchanged up sequence must re-arm the button.
        tap(session,flow.NEXT)
        self.assertEqual(controller.commands.get_nowait()['step'],4)
        controller.detach()

    def test_drag_multitouch_and_offline_contact_do_not_advance(self):
        directory,session,controller = fixture('reject_contacts')
        tick(session)
        x,y = (flow.NEXT[0]+flow.NEXT[2])//2,(flow.NEXT[1]+flow.NEXT[3])//2
        session.hardware.event(x,y,True);tick(session)
        session.hardware.event(500,400,True);tick(session)
        session.hardware.event(x,y,False);tick(session)
        self.assertTrue(controller.commands.empty())
        session.hardware.event(x,y,True,points=2);tick(session)
        session.hardware.event(x,y,False);tick(session)
        self.assertTrue(controller.commands.empty())
        x,y = (flow.NEXT[0]+flow.NEXT[2])//2,(flow.NEXT[1]+flow.NEXT[3])//2
        session.hardware.event(x,y,True);tick(session)
        session.hardware.event(x,y,True,online=False);tick(session)
        session.hardware.event(x,y,False);tick(session)
        self.assertTrue(controller.commands.empty())
        tap(session,flow.NEXT)
        self.assertEqual(controller.commands.get_nowait()['action'],'execute')
        controller.detach()

    def test_back_and_retry_go_to_preparation_not_immediate_capture(self):
        directory,session,controller = fixture('back_retry')
        controller.set_phase('review',4);tick(session)
        tap(session,flow.BACK)
        self.assertEqual(controller.commands.get_nowait(),dict(action='prepare',step=3,answer=None))
        controller.set_phase('failed',7);tick(session)
        tap(session,flow.RETRY)
        self.assertEqual(controller.commands.get_nowait()['step'],7)
        controller.detach()

    def test_preview_is_ui_owned_and_returns_to_camera_before_execute(self):
        directory,session,controller = fixture('preview_camera')
        controller.set_phase('busy',4)
        controller.display(Image.new('RGB',(800,480),'red'))
        self.assertIsNone(session.hardware.front)
        controller.set_phase('review',4);tick(session)
        self.assertIsNotNone(session.hardware.front)
        tap(session,flow.NEXT)
        self.assertIsNone(session.hardware.front)
        self.assertEqual(controller.commands.get_nowait()['step'],5)
        controller.detach()

    def test_calibration_collects_four_taps_and_preserves_abi87(self):
        directory,session,controller = fixture('calibration')
        session.viewer.matrix = None
        tick(session)
        for x,y in viewer_module.TARGETS.astype(int):
            session.hardware.event(int(x),int(y),True);tick(session)
            session.hardware.event(int(x),int(y),False);tick(session)
        self.assertIsNotNone(session.viewer.matrix)
        saved = json.loads(session.viewer.cal_path.read_text(encoding='utf8'))
        self.assertEqual(saved['version'],87)
        np.testing.assert_allclose(saved['matrix'],[[1,0],[0,1],[0,0]],atol=1e-10)
        controller.detach()

    def test_speed_retry_and_manual_timing_are_touch_answers(self):
        directory,session,controller = fixture('ask')
        answers = []
        error = []
        def ask():
            try:
                answers.append(controller.ask('输入r重试自动测速，m使用原手动计时，q取消：'))
            except BaseException as value:
                error.append(value)
        worker = threading.Thread(target=ask)
        worker.start()
        deadline = time.monotonic()+2
        while controller.phase!='prompt' and time.monotonic()<deadline:
            time.sleep(.01)
        tick(session);tap(session,flow.NEXT)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(error)
        self.assertEqual(answers,['r'])
        with self.assertRaisesRegex(RuntimeError,'尚未映射'):
            controller.ask('请输入任意数字')
        controller.detach()

    def test_manual_start_stop_retime_and_cancel_use_original_prompts(self):
        directory,session,controller = fixture('manual_answers')
        cases = [
            ('记号到指定位置时按回车开始；输入 q 回车取消：',flow.NEXT,''),
            ('记号再经过同一位置 3 次后按回车；没测好输入 r 回车重测，q 取消：',flow.NEXT,''),
            ('记号再经过同一位置 3 次后按回车；没测好输入 r 回车重测，q 取消：',flow.RETRY,'r'),
            ('记号到指定位置时按回车开始；输入 q 回车取消：',flow.EXIT,'q'),
            ('输入r重试自动测速，m使用原手动计时，q取消：',flow.PREVIEW,'m'),
        ]
        for prompt,rect,expected in cases:
            answers = []
            worker = threading.Thread(target=lambda:answers.append(controller.ask(prompt)))
            worker.start()
            deadline = time.monotonic()+2
            while controller.phase!='prompt' and time.monotonic()<deadline:
                time.sleep(.005)
            tick(session);tap(session,rect)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(answers,[expected])
        controller.detach()

    def test_model_button_uses_existing_viewer_and_keeps_model_header(self):
        directory,session,controller = fixture('view_model')
        model = directory/'model.ply';model.write_text('fixture',encoding='ascii')
        session.state.update(status='completed',step=8,model_path=str(model),volumes=[12.])
        controller.set_phase('review',8);tick(session)
        tap(session,flow.MODEL)
        self.assertEqual(session.viewer.mode,'view')
        self.assertTrue((session.canvas.pixels[416:]==0).all())
        time.sleep(.03);tick(session)
        self.assertIsNotNone(session.hardware.front)
        # Exercise the real v95 viewer through the workflow, with fake hardware.
        # Reset only poll timestamps so a test need not sleep between samples.
        def model_tap(rect):
            x,y = (rect[0]+rect[2])//2,(rect[1]+rect[3])//2
            for down in (True,False):
                session.viewer.last_poll = 0.
                session.hardware.event(x,y,down);tick(session)
        model_tap(viewer_module.PLUS)
        self.assertEqual(session.viewer.zoom,1.25)
        model_tap(viewer_module.MINUS)
        self.assertEqual(session.viewer.zoom,1.)
        model_tap(viewer_module.BACK)
        self.assertEqual(session.viewer.mode,'result')
        self.assertIsNotNone(session.workflow)
        tap(session,flow.RETRY)
        self.assertEqual(controller.commands.get_nowait()['action'],'prepare')
        controller.detach()

    def test_invalidation_clears_downstream_and_cancelled_speed_is_not_success(self):
        namespace = {name:'old' for names in flow.OUTPUTS.values() for name in names}
        flow.invalidate(namespace,3)
        self.assertIn('tt565',namespace)
        self.assertNotIn('cal',namespace)
        self.assertNotIn('report',namespace)
        with self.assertRaises(RuntimeError):
            flow.validate_result(dict(period_s=None),5)

    def test_runtime_python36_and_ui_acquisition_driver_ast_unchanged(self):
        for path in (HERE/'runtime').glob('*.py'):
            ast.parse(path.read_text(encoding='utf8'),feature_version=(3,6))
        old = ast.parse((ROOT/'sim/reference/scan_screen_v88.py').read_text(encoding='utf8'))
        new = ast.parse((HERE/'runtime/scan_screen.py').read_text(encoding='utf8'))
        for name in ('BramWriter','ui_mapping','render'):
            before = next(node for node in old.body if getattr(node,'name',None)==name)
            after = next(node for node in new.body if getattr(node,'name',None)==name)
            self.assertEqual(ast.dump(before),ast.dump(after))

    def test_sidebar_geometry_separates_header_tips_buttons_and_preview(self):
        rectangles = [flow.NEXT,flow.PREVIEW,flow.RETRY,flow.BACK,flow.EXIT]
        self.assertGreaterEqual(flow.PANEL[1],72)
        self.assertLessEqual(flow.VIEWPORT[2],flow.PANEL[0]-8)
        self.assertGreaterEqual(flow.TIP[1],flow.PANEL[1])
        self.assertLess(flow.TIP[3],min(rect[1] for rect in rectangles))
        for rect in rectangles:
            self.assertGreaterEqual(rect[0],flow.PANEL[0])
            self.assertGreaterEqual(rect[1],64+8)
            self.assertLessEqual(rect[2],800)
            self.assertLessEqual(rect[3],480)
            self.assertGreaterEqual(rect[2]-rect[0],140)
            self.assertGreaterEqual(rect[3]-rect[1],48)
        for first,second in zip(rectangles,rectangles[1:]):
            self.assertGreaterEqual(second[1]-first[3],8)

    def test_controls_only_write_sidebar_keep_header_and_bottom_left_visible(self):
        directory,session,controller = fixture('panel_only')
        session.canvas.pixels.fill(0)
        controller._draw_controls(session.canvas,True)
        allowed = np.zeros((480,800),bool)
        x0,y0,x1,y1 = flow.PANEL
        allowed[y0:y1,x0:x1] = True
        self.assertFalse(session.canvas.pixels[~allowed].any())
        self.assertFalse(session.canvas.pixels[:64].any())
        self.assertFalse(session.canvas.pixels[386:,:632].any())
        self.assertFalse(session.canvas.missing)
        controller.detach()

    def test_all_preparation_review_and_manual_timing_tips_fit_sidebar(self):
        canvas = screen.BitmapCanvas()
        messages = list(flow.PROMPTS.values())+list(flow.REVIEW.values())+[
            '触屏未连接，等待恢复','本步未完成，可重试或返回上一步',
            '模型暂未打开，可重试；原结果已保存','正在处理，请等候；步骤按钮已锁定',
            '自动测速未通过，请选择处理方式','标记到指定位置时，点击开始计时',
            '标记再经过同一位置3次后，点击结束计时']
        for message in messages:
            lines = flow.wrapped_lines(canvas,message)
            self.assertEqual(''.join(lines),message)
            self.assertLessEqual(len(lines),5)
            self.assertLessEqual(flow.TIP[1]+(len(lines)-1)*21+18,flow.TIP[3])
            for line in lines:
                advance = canvas.text(0,0,line,18,1,1000)
                self.assertLessEqual(advance,flow.TIP[2]-flow.TIP[0])
        self.assertFalse(canvas.missing)

    def test_preview_preserves_aspect_ratio_and_stays_left_of_sidebar(self):
        directory,session,controller = fixture('preview_left')
        captured = []
        previous = session.hardware.present
        def present(frame):
            captured.append(frame.copy())
            previous(frame)
        session.hardware.present = present
        source = Image.new('RGB',(800,480),'red')
        controller.display(source)
        controller.set_phase('review',4);tick(session)
        self.assertEqual(source.size,(800,480))
        frame = captured[-1]
        yy,xx = np.nonzero(frame==0xf800)
        self.assertEqual((int(xx.min()),int(xx.max())),(0,631))
        height = int(yy.max()-yy.min()+1)
        self.assertLessEqual(abs(632./height-800./480),.01)
        self.assertTrue((frame[:64]==0x1082).all())
        self.assertTrue((frame[:,632:]==0x1082).all())
        tap(session,flow.NEXT)
        self.assertIsNone(session.hardware.front)
        controller.detach()

    def test_obsolete_footer_on_left_has_no_hidden_buttons(self):
        directory,session,controller = fixture('old_footer')
        tick(session)
        for rect in [(8,416,144,472),(152,416,288,472),(296,416,432,472)]:
            tap(session,rect)
        self.assertTrue(controller.commands.empty())
        tap(session,flow.NEXT)
        self.assertEqual(controller.commands.get_nowait()['action'],'execute')
        controller.detach()

    def test_scanning_startup_and_nonlayout_control_logic_ast_unchanged(self):
        old = ast.parse((ROOT/'sim/reference/touch_scan_v105.py').read_text(encoding='utf8'))
        new = ast.parse((HERE/'runtime/touch_scan.py').read_text(encoding='utf8'))
        for name in ('load_cells','invalidate','validate_result'):
            before = next(node for node in old.body if getattr(node,'name',None)==name)
            after = next(node for node in new.body if getattr(node,'name',None)==name)
            self.assertEqual(ast.dump(before),ast.dump(after))
        old_class = next(node for node in old.body if getattr(node,'name',None)=='Controller')
        new_class = next(node for node in new.body if getattr(node,'name',None)=='Controller')
        for name in ('install','detach','set_phase','display','_camera','_queue',
                     '_tap','_calibrate','ask'):
            before = next(node for node in old_class.body if getattr(node,'name',None)==name)
            after = next(node for node in new_class.body if getattr(node,'name',None)==name)
            self.assertEqual(ast.dump(before),ast.dump(after))


def fake_run(fail_step=None):
    directory = RUN/('full_flow_failure' if fail_step else 'full_flow')
    directory.mkdir()
    session = Session(directory)
    environment = types.ModuleType('fake_touch_environment')
    environment.session = session
    environment.log = []
    environment.fail_step = fail_step
    environment.failed = False
    environment.require_framebuffer_reservation = lambda:environment.log.append('reserved')
    environment.Overlay = lambda:environment.log.append('overlay')
    @contextmanager
    def step(index,scope):
        session.state.update(step=index,status='running')
        environment.log.append(('step',index,session.hardware.front))
        yield
        session.state.update(status='waiting')
    fake_screen = types.SimpleNamespace(VERSION=106,_SESSION=session,step=step,render=screen.render,
                                       ask=lambda *a:None,detach=lambda:environment.log.append('UNEXPECTED_DETACH'))
    environment.screen = fake_screen
    sys.modules[environment.__name__] = environment
    initial = ("from pathlib import Path\nimport fake_touch_environment as env\n"
               "from fake_touch_environment import require_framebuffer_reservation, Overlay\n"
               "require_framebuffer_reservation()\nol_color=Overlay()\n"
               "scan_screen=env.screen\ndisplay=lambda *a,**k:None\nRUN_DIR=Path(%r)\n"%str(directory))
    bodies = {
        1:'bg565=1; camera_settings={}; exposure_bank={}\n',
        2:'tt565=2; background_sigma=3\n',
        3:"if env.fail_step==3 and not env.failed:\n    env.failed=True\n    raise ValueError('controlled calibration failure')\ncal={}\n",
        4:'preview={}\n',5:'period_s=20.; speed_info={}\n',6:'color_scan={}\n',
        7:'scan={}\n',8:"report={'volume_cm3':12.}; result_dir=RUN_DIR\n",
    }
    cells = [dict(cell_type='code',source=initial)]
    for index in range(1,9):
        source = ('scan_screen.detach()\n' if index==8 else '')+'with scan_screen.step(%d, globals()):\n'%index
        source += ''.join('    '+line+'\n' for line in bodies[index].splitlines())
        cells.append(dict(cell_type='code',source=source))
    notebook = directory/'original.ipynb'
    notebook.write_text(json.dumps(dict(cells=cells)),encoding='utf8')
    previous = flow.__file__
    previous_guard = sys.modules.get('scan_pl_guard')
    sys.modules['scan_pl_guard'] = types.SimpleNamespace(__file__=str(directory/'scan_pl_guard.py'),
        install=lambda:environment.log.append('rpc_guard'),
        healthcheck=lambda:environment.log.append('rpc_health'),save=lambda *a:None)
    flow.__file__ = str(directory/'touch_scan.py')
    scope = dict(report={'volume_cm3':999},cal={'stale':True})
    errors = []
    def user():
        try:
            deadline = time.monotonic()+10
            phase_key = None
            while time.monotonic()<deadline:
                controller = session.workflow
                if controller is not None:
                    tick(session)
                    key = (controller.phase,controller.step,controller.epoch)
                    if key != phase_key and controller.phase in ('ready','review','failed'):
                        phase_key = key
                        if controller.phase=='review' and controller.step==8:
                            tap(session,flow.EXIT)
                            return
                        tap(session,flow.NEXT)
                time.sleep(.005)
            raise AssertionError('Fake user timed out')
        except BaseException as error:
            errors.append(error);session.stop.set()
    worker = threading.Thread(target=user)
    worker.start()
    try:
        flow.run(scope,notebook)
    finally:
        worker.join(3)
        flow.__file__ = previous
        if previous_guard is None: sys.modules.pop('scan_pl_guard',None)
        else: sys.modules['scan_pl_guard'] = previous_guard
    assert not errors,errors
    assert not worker.is_alive()
    assert scope['report']['volume_cm3']==12 and 'stale' not in scope['cal']
    assert environment.log[:4]==['rpc_guard','rpc_health','reserved','overlay']
    assert 'UNEXPECTED_DETACH' not in environment.log
    rows = [row for row in environment.log if isinstance(row,tuple)]
    expected = [1,2,3,3,4,5,6,7,8] if fail_step else list(range(1,9))
    assert [row[1] for row in rows]==expected
    assert all(row[2] is None for row in rows)
    assert session.workflow is None
    return dict(steps=[row[1] for row in rows],notebook_only_one_start=True,
                hardware_operations='fake_only',retry_failure=bool(fail_step),passed=True)


if __name__=='__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    integrations = []
    if result.wasSuccessful():
        integrations = [fake_run(),fake_run(3)]
    report = dict(unit_tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
                  integrations=integrations,board_tested=False,actual_scanner_run=False)
    (WORK/'touch_checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    sys.exit(0 if result.wasSuccessful() and len(integrations)==2 else 1)
