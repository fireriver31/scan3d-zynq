"""Physical TFT OSD: teammate's 800x480 / 2bpp ui_mixer protocol.

No camera DDR access, Overlay download, image segmentation or model mutation.
Progress producers update RAM only. One bounded-rate writer owns the UI BRAM.
Python 3.6 / NumPy; Chinese glyphs are bundled, no system font installation.
"""
from contextlib import contextmanager
from pathlib import Path
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
import numpy as np

VERSION = 88
WIDTH, HEIGHT = 800, 480
STRIP_HEIGHT = 64
WORDS_PER_ROW, VISIBLE_WORDS = 50, 24000
UI_BASE, UI_SIZE = 0x40000000, 0x20000
TRANSPARENT, WHITE, BLACK, ACCENT = 0, 1, 2, 3
STEP_NAMES = ('初始化', '无转台背景', '空转台参考', '转台标定', '单张掩膜',
              '测量转速', '采集原图', '生成掩膜', '三维重建')
STATUS_TEXT = dict(ready='准备就绪', running='运行中', waiting='等待操作',
                   completed='计算完成', failed='处理失败', interrupted='已中断')
if '_SESSION' not in globals():
    _SESSION = None
if '_FAILED_OVERLAY' not in globals():
    _FAILED_OVERLAY = None
if '_LAST_ATTACH' not in globals():
    _LAST_ATTACH = None
if '_HELD_SESSIONS' not in globals():
    _HELD_SESSIONS = []


def _number(value):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError('体积不是有效数值')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError('体积必须是有限非负数')
    return result


def _integer(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def _volume_text(value):
    text = '%.2f' % value
    return text if len(text) <= 9 else '%.3g' % value


def _loaded(overlay):
    check = getattr(overlay, 'is_loaded', None)
    return bool(check() if callable(check) else check)


def ui_mapping(overlay, hwh_path=None):
    """Prove address and span from metadata before constructing any MMIO.

    Old HWH has a 4KB ADDRESSBLOCK but a verified 128KB MEMRANGE. Accept the
    matching file's explicit MEMRANGE when the older parser reports only 4KB.
    Never guess the address when ui_bram is absent from the loaded overlay.
    """
    if not _loaded(overlay):
        raise RuntimeError('当前Overlay未加载，不访问UI地址')
    info = getattr(overlay, 'ip_dict', {}).get('ui_bram')
    if not isinstance(info, dict):
        raise RuntimeError('当前Overlay没有ui_bram，实体屏UI未启用')
    bases = [_integer(info[k]) for k in ('phys_addr', 'base_address', 'physical_addr', 'base_addr')
             if info.get(k) is not None]
    if bases and any(v != UI_BASE for v in bases):
        raise RuntimeError('ui_bram地址与当前800x480混合器不匹配')
    base = bases[0] if bases else None
    spans = [_integer(info[k]) for k in ('addr_range', 'range', 'size') if info.get(k) is not None]
    if spans and max(spans) >= UI_SIZE and base == UI_BASE:
        return base, UI_SIZE
    bitfile = getattr(overlay, 'bitfile_name', None)
    path = Path(hwh_path) if hwh_path is not None else Path(bitfile).with_suffix('.hwh') if bitfile else None
    if path is None or not path.is_file():
        raise RuntimeError('UI映射不足128KB，且找不到匹配HWH的MEMRANGE')
    if bitfile and path.resolve() != Path(bitfile).with_suffix('.hwh').resolve():
        raise RuntimeError('提供的UI硬件描述不属于已加载的bit')
    root = ET.parse(str(path)).getroot()
    matches = [e for e in root.iter() if e.tag.split('}')[-1] == 'MEMRANGE'
               and e.get('INSTANCE') == 'ui_bram' and e.get('BASEVALUE') and e.get('HIGHVALUE')
               and _integer(e.get('BASEVALUE')) == UI_BASE
               and _integer(e.get('HIGHVALUE')) + 1 >= UI_BASE + UI_SIZE]
    if len(matches) != 1:
        raise RuntimeError('匹配HWH未声明唯一的128KB UI存储区')
    return UI_BASE, UI_SIZE


class BitmapCanvas:
    """Chinese bitmap drawing, same palette and least-significant-pixel order."""
    def __init__(self):
        import scan_screen_font
        self.font = scan_screen_font
        self.pixels = np.zeros((HEIGHT, WIDTH), np.uint8)
        self.cache = {}
        self.missing = set()

    def rect(self, x0, y0, x1, y1, colour):
        self.pixels[max(0, y0):min(HEIGHT, y1), max(0, x0):min(WIDTH, x1)] = colour

    def glyph(self, char, size):
        key = (char, size)
        if key not in self.cache:
            raw = self.font.packed(char)
            if raw is None:
                self.missing.add(char)
                raw = self.font.packed('?')
            bits = np.unpackbits(np.frombuffer(raw, np.uint8)).reshape(32, 32)
            # Preserve a proportional advance for Latin/numeric glyphs.
            if ord(char) < 128 or char in '³²':
                xs = np.flatnonzero(bits.any(axis=0))
                bits = bits[:, xs[0]:xs[-1]+1] if len(xs) else bits[:, :10]
            h, w = bits.shape
            nh, nw = int(size), max(1, int(round(w * size / 32.)))
            y = np.minimum((np.arange(nh) * h / nh).astype(int), h-1)
            x = np.minimum((np.arange(nw) * w / nw).astype(int), w-1)
            self.cache[key] = bits[y[:, None], x[None, :]].astype(bool)
        return self.cache[key]

    def text(self, x, y, text, size=24, colour=WHITE, max_width=760):
        start = x
        for char in str(text):
            if char == ' ':
                x += max(4, size//3)
                continue
            glyph = self.glyph(char, size)
            h, w = glyph.shape
            if x + w > min(WIDTH, start+max_width):
                break
            y0, x0, y1, x1 = max(0, y), max(0, x), min(HEIGHT, y+h), min(WIDTH, x+w)
            if x1 > x0 and y1 > y0:
                patch = self.pixels[y0:y1, x0:x1]
                patch[glyph[y0-y:y1-y, x0-x:x1-x]] = colour
            x += w + max(1, size//12)
        return x-start


def _duration(seconds):
    value = max(0, int(seconds))
    return '%02d:%02d' % (value//60, value % 60)


def render(canvas, state, now=None):
    """One compact top strip; the remaining 416 rows show the camera."""
    now = time.monotonic() if now is None else now
    px = canvas.pixels
    px.fill(TRANSPARENT)
    canvas.rect(0, 0, WIDTH, STRIP_HEIGHT, BLACK)
    canvas.text(10, 4, '三维扫描', 22, ACCENT, 110)
    elapsed = state.get('finished_at') or now
    canvas.text(412, 6, '用时 ' + _duration(elapsed-state['started_at']), 18, WHITE, 126)
    canvas.text(545, 6, STATUS_TEXT[state['status']], 18, ACCENT, 78)
    step = int(state['step'])
    heading = '初始化' if step == 0 else '第%d/8步  %s' % (step, STEP_NAMES[step])
    canvas.text(130, 4, heading, 22, WHITE, 274)
    stage = state.get('stage') or STEP_NAMES[step]
    frame = state.get('frame') or ''
    # Put the real frame count first so long stage descriptions cannot hide it.
    detail = (frame + '  ' + stage) if frame else (state.get('detail') or stage)
    canvas.text(10, 36, detail, 20, WHITE, 615)
    canvas.rect(633, 6, 635, 58, ACCENT)
    volumes = state.get('volumes', [])
    canvas.text(645, 3, '体积估计 cm³', 16, WHITE, 149)
    value = _volume_text(volumes[0]) if volumes else '--.--'
    canvas.text(645, 27, value, 30 if len(value) <= 8 else 25, ACCENT, 149)
    if state.get('model_path') and state['status']=='completed':
        canvas.rect(408, 0, 628, 64, BLACK)
        if state.get('touch_online'):
            from tft_viewer import button, VIEW
            button(canvas, VIEW, '查看模型')
        else:
            canvas.text(418, 12, '模型查看失败' if state.get('model_viewer_error') else '触屏未连接', 22, ACCENT, 206)
            canvas.text(418, 40, '请检查接线', 16, WHITE, 206)
    return px


class BramWriter:
    """Only 0..95999 in the separately proven UI BRAM are writable here."""
    def __init__(self, overlay, mmio):
        self.overlay, self.mmio = overlay, mmio
        self.previous = np.full((HEIGHT, WIDTH), 255, np.uint8)
        self.shadow = np.full(VISIBLE_WORDS, -1, np.int64)
        self.writes = 0

    def flush(self, pixels):
        if not _loaded(self.overlay):
            raise RuntimeError('Overlay已更换，停止旧UI写入')
        pixels = np.asarray(pixels)
        if pixels.shape != (HEIGHT, WIDTH) or pixels.dtype != np.uint8 or (pixels > 3).any():
            raise ValueError('UI画布必须为480x800的2bit索引图')
        rows = np.flatnonzero((pixels != self.previous).any(axis=1))
        shifts = 2*np.arange(16, dtype=np.uint32)
        written = 0
        for index, y in enumerate(rows):
            words = np.bitwise_or.reduce(pixels[y].reshape(50, 16).astype(np.uint32) << shifts, axis=1)
            first = int(y)*50
            changed = np.flatnonzero(words != self.shadow[first:first+50])
            if index and index % 64 == 0 and not _loaded(self.overlay):
                raise RuntimeError('Overlay已更换，停止旧UI写入')
            for x in changed:
                self.mmio.write(4*(first+int(x)), int(words[x]))
            written += len(changed)
            self.shadow[first:first+50] = words
            self.previous[y] = pixels[y]
        self.writes += written
        return written

    def verify(self):
        """Check distinct rendered words through the real AXI read interface.

        This verifies PS write/read access, not the pixel-clock-side mixer or
        the physical panel. Never write a guessed address or a scratch area.
        """
        if not _loaded(self.overlay):
            raise RuntimeError('Overlay已更换，取消UI回读')
        indices = {0, 49, 50, 2699, 2700, 16299, 16300, 16349, 17650,
                   19200, 21950, 23999}
        # Include letter pixels as well as the solid/transparent panels.
        letters = np.flatnonzero((self.shadow >= 0) & (self.shadow != 0) &
                                 (self.shadow != 0xAAAAAAAA))
        if len(letters):
            indices.update(int(letters[i]) for i in np.linspace(
                0, len(letters)-1, min(12, len(letters))).astype(int))
        for index in sorted(indices):
            expected = int(self.shadow[index])
            if expected < 0:
                raise RuntimeError('UI尚未完成首帧写入')
            actual = int(self.mmio.read(index*4)) & 0xFFFFFFFF
            if actual != expected:
                raise RuntimeError('UI回读不一致：偏移0x%05X，写入0x%08X，读到0x%08X' %
                                   (index*4, expected, actual))
        return dict(passed=True, sampled_words=len(indices),
                    display_confirmed=False, checked_offsets=[i*4 for i in sorted(indices)])


class ScreenSession:
    def __init__(self, overlay, mmio, directory=None, clock=time.monotonic):
        self.overlay = overlay
        self.writer = BramWriter(overlay, mmio)
        self.canvas = BitmapCanvas()
        self.directory = Path(directory) if directory is not None else None
        self.clock = clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.error = None
        self.readback = None
        self.state = dict(step=0, status='ready', stage='共用硬件已加载', frame='',
                          photo_frame='',detail='下一步：无转台背景', volumes=[], started_at=clock(), finished_at=None)
        self.report_before = None
        self.last_render_state = None
        self.last_saved_state = None
        self.viewer_lock = threading.RLock()
        self.viewer = None
        self.viewer_error = None
        self.screen_lease = None
        self.last_save_time = 0.
        self.state['model_path'] = None
        if ('touch_viewer_0' in getattr(overlay, 'ip_dict', {}) or
            'touch_viewer_0' in getattr(overlay, 'hierarchy_dict', {}) or
            Path(str(getattr(overlay, 'bitfile_name', ''))).name == '1026_touch_model_v87.bit'):
            try:
                from tft_viewer import Viewer
                self.viewer = Viewer(overlay, directory)
            except Exception as exc:
                self.viewer_error = str(exc)
                print('触屏查看器未启用：%s；扫描和体积UI继续。' % exc, flush=True)

    def snapshot(self):
        with self.lock:
            return dict(self.state, volumes=list(self.state['volumes']), ui_version=VERSION,
                        ui_error=self.error, model_viewer_error=self.viewer_error, bram_readback=self.readback,
                        display_confirmed=False, camera_frame_modified=False)

    def _save(self):
        if self.directory is None:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory/'1024_screen_status_v68.json'
            temp = path.with_suffix('.tmp')
            payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
            if payload == self.last_saved_state:
                return
            temp.write_text(payload, encoding='utf8')
            temp.replace(path)
            self.last_saved_state = payload
        except OSError:
            pass

    def draw_once(self):
        # Only the rendering thread (or the explicit offline test) calls this.
        state, now = self.snapshot(), self.clock()
        state['touch_online'] = bool(self.viewer and self.viewer.touch and self.viewer.touch['online'])
        state['model_viewer_error'] = self.viewer_error
        key = (json.dumps(state, sort_keys=True), int((state['finished_at'] or now)-state['started_at']))
        if key == self.last_render_state:
            return 0
        count = self.writer.flush(render(self.canvas, state, now))
        self.last_render_state = key
        return count

    def _run(self):
        while not self.stop.wait(.025 if self.state.get('model_path') else 1.):
            try:
                owned = False
                with self.viewer_lock:
                    if self.viewer is not None:
                        try:
                            owned = self.viewer.tick(self.canvas, self.writer, self.snapshot(), self.stop)
                        except Exception as exc:
                            self.viewer_error = str(exc)
                            self.viewer.error = self.viewer_error
                            try:
                                self.viewer._save()
                            except Exception:
                                pass
                            try:
                                self.viewer.close()
                            except Exception as closing_error:
                                # Keep the failed viewer and its OS lease alive.
                                # A second close must not pretend DMA drained.
                                raise RuntimeError('模型显示停止但旧读取未确认结束：%s'%closing_error)
                            self.viewer = None
                            self.last_render_state = None
                            print('实体屏模型查看已停止：%s；原扫描结果保持。' % exc, flush=True)
                    if owned:
                        self.last_render_state = None
                    else:
                        self.draw_once()
                if self.clock()-self.last_save_time >= 1.:
                    self._save()
                    self.last_save_time = self.clock()
            except Exception as error:
                self.error = str(error)
                self.stop.set()
                self._save()
                print('实体屏UI已停止：%s；Notebook计算不受影响。' % error, flush=True)

    def start(self):
        self.draw_once()  # Clear stale BRAM contents before new acquisition.
        self.readback = self.writer.verify()
        self._save()
        self.worker = threading.Thread(target=self._run, name='scan-screen-ui')
        self.worker.daemon = True
        self.worker.start()

    def begin(self, step, scope):
        with self.viewer_lock:
            if self.viewer is not None:
                self.viewer.reset()
            # The worker must never see the old completed model after reset.
            with self.lock:
                self.state['model_path'] = None
                self.report_before = scope.get('report')
                self.state.update(step=step, status='running', stage=STEP_NAMES[step],
                                  frame='', photo_frame='',detail='', volumes=[], started_at=self.clock(), finished_at=None)

    def finish(self, step, scope, error=None):
        with self.lock:
            if error is not None:
                status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
                self.state.update(status=status, stage='请查看Notebook报错', frame='',
                                  detail='中断或失败后可重跑当前步骤', volumes=[], finished_at=self.clock())
            else:
                self.state.update(status='waiting', frame='', finished_at=self.clock())
                self.state['stage'] = '本步骤已完成'
                self.state['detail'] = '下一步：'+STEP_NAMES[min(step+1, 8)]
                if step == 5 and scope.get('period_s') is None:
                    self.state.update(stage='转速测量已取消', detail='重跑第5步后再继续')
                if step == 8:
                    report = scope.get('report')
                    if isinstance(report, dict) and report is not self.report_before and 'volume_cm3' in report:
                        try:
                            volumes = [_number(report['volume_cm3'])]
                        except (ValueError, TypeError, OverflowError):
                            self.state.update(status='failed', stage='体积结果无效', detail='请查看本次result.json')
                        else:
                            self.state.update(status='completed', volumes=volumes,
                                stage='模型已保存，形状仍需核对',
                                detail='体积为视觉外壳估计')
                            name = report.get('detail_mesh_file')
                            result_dir = scope.get('result_dir')
                            if name and result_dir:
                                base = Path(result_dir).resolve()
                                model = (base / name).resolve()
                                if model.parent == base and model.is_file():
                                    self.state['model_path'] = str(model)
                    else:
                        self.state.update(stage='尚未生成本次结果', detail='先完成第7步掩膜检查')

    def progress(self, snapshot, directory=None):
        # Nonblocking producer: no drawing, file write, MMIO or image copy.
        with self.lock:
            if self.state['status'] not in ('running', 'waiting'):
                return
            expected = {4: ('PL', 'PS'), 6: ('capture',), 7: ('PL', 'PS'), 8: ('reconstruction',)}
            if snapshot.get('segmentation_backend') not in expected.get(self.state['step'], ()):
                return
            frame = str(snapshot.get('frame') or '')
            backend = snapshot.get('segmentation_backend')
            photo = re.search(r'照片\s*(\d+)\s*/\s*(\d+)', frame)
            if photo:
                self.state['photo_frame'] = '照片 %s/%s' % photo.groups()
            elif backend == 'capture' and re.match(r'^\d+/\d+$', frame):
                self.state['photo_frame'] = '照片 '+frame
            elif '首尾帧' in frame:
                self.state['photo_frame'] = frame
            elif self.state['photo_frame'] and backend in ('PL', 'PS'):
                phase = frame.replace('foreground', '前景').replace('background', '背景')
                frame = self.state['photo_frame']+('  '+phase if phase else '')
            if backend == 'capture' and self.state['photo_frame']:
                frame = self.state['photo_frame']
            self.state.update(stage=str(snapshot.get('stage') or STEP_NAMES[self.state['step']]),
                              frame=frame, status='running')

    def input_wait(self, prompt):
        with self.lock:
            self.state.update(status='waiting', stage='等待Notebook输入', frame='', detail=str(prompt))

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=3.)
            if self.worker.is_alive():
                raise RuntimeError('旧UI写入线程未停止；暂不允许重新加载Overlay')
        with self.viewer_lock:
            if self.viewer is not None:
                self.viewer.close()
                self.viewer = None
        if self.screen_lease is not None:
            self.screen_lease.close()
            self.screen_lease = None


def detach():
    global _SESSION
    if _SESSION is not None:
        _SESSION.close()
        _SESSION = None


def _save_attach(directory, record):
    if directory is None:
        return
    try:
        path = Path(directory)/'1024_screen_attach_v68.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding='utf8')
    except OSError as error:
        print('实体屏UI诊断未保存：%s' % error, flush=True)


def _refresh_touch_modules():
    import importlib
    for name in ('tft_touch','tft_model','tft_viewer'):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve().parent != Path(__file__).resolve().parent:
            raise RuntimeError('触屏配套模块不在当前Notebook目录：'+name)
        importlib.reload(module)


def attach(overlay, directory=None, hwh_path=None):
    """Attach to already-loaded hardware. UI failure never replaces processing."""
    global _SESSION, _FAILED_OVERLAY, _LAST_ATTACH
    detach()
    record = dict(ui_version=VERSION, module_path=str(Path(__file__).resolve()),
                  phase='metadata', passed=False, display_confirmed=False,
                  bitfile=str(getattr(overlay, 'bitfile_name', '')),
                  supplied_hwh=str(hwh_path or ''),
                  ui_ip=getattr(overlay, 'ip_dict', {}).get('ui_bram'),
                  ip_names=sorted(getattr(overlay, 'ip_dict', {})), error=None)
    session = None
    lease = None
    try:
        base, span = ui_mapping(overlay, hwh_path)
        # Acquire before any MMIO write, including the viewer constructor's
        # camera handoff. A second process cannot steal an active CMA frame.
        from screen_lease import ScreenLease
        lease = ScreenLease()
        # detach() above joined the old UI owner; now refresh its dependencies.
        _refresh_touch_modules()
        record.update(base=base, span=span, phase='MMIO')
        from pynq import MMIO
        session = ScreenSession(overlay, MMIO(base, span), directory)
        session.screen_lease = lease
        record['phase'] = 'draw_and_readback'
        session.start()
    except Exception as error:
        if session is not None:
            try:
                session.close()
            except Exception as closing_error:
                _HELD_SESSIONS.append(session)
                record['close_error'] = str(closing_error)
        elif lease is not None:
            lease.close()
        _FAILED_OVERLAY = overlay
        record.update(error='%s: %s' % (type(error).__name__, error))
        _LAST_ATTACH = record
        _save_attach(directory, record)
        print('实体屏UI未启用 [%s]：%s；当前计算继续，下一格将重新尝试。' %
              (record['phase'], error), flush=True)
        return None
    _SESSION = session
    _FAILED_OVERLAY = None
    record.update(passed=True, phase='write_readback_passed', readback=session.readback)
    _LAST_ATTACH = record
    _save_attach(directory, record)
    print('实体屏UI v%d：已写入ui_bram，%d个位置回读一致；请核对实体屏是否出现黑底黄字。' %
          (VERSION, session.readback['sampled_words']), flush=True)
    return session


def ensure_attached(scope):
    """Adding UI mid-session never reloads the FPGA or recaptures a reference."""
    overlay = scope.get('ol_color')
    if overlay is None:
        print('实体屏UI未启用：当前内核没有ol_color；没有重新加载bit。', flush=True)
        return None
    if _SESSION is None or _SESSION.overlay is not overlay or _SESSION.stop.is_set():
        bit = scope.get('COLOR_BIT')
        return attach(overlay, scope.get('RUN_DIR'), Path(bit).with_suffix('.hwh') if bit else None)
    return _SESSION


def publish_progress(snapshot, directory=None):
    session = _SESSION
    if session is not None and not session.stop.is_set():
        try:
            session.progress(snapshot, directory)
        except Exception as error:
            session.error = str(error)
            session.stop.set()
            print('实体屏UI进度更新失败：%s；计算继续。' % error, flush=True)


def ask(prompt='', reader=None):
    if _SESSION is not None:
        _SESSION.input_wait(prompt)
    try:
        return (input if reader is None else reader)(prompt)
    finally:
        if _SESSION is not None:
            with _SESSION.lock:
                _SESSION.state.update(status='running', stage=STEP_NAMES[_SESSION.state['step']], detail='')


@contextmanager
def step(number, scope):
    """Same notebook globals and exception semantics; no new function scope."""
    ensure_attached(scope)
    session = _SESSION
    if session is not None:
        session.begin(number, scope)
    try:
        yield
    except BaseException as error:
        if session is not None:
            session.finish(number, scope, error)
        raise
    else:
        if session is not None:
            session.finish(number, scope)
