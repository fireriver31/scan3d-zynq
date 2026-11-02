"""Local board acceptance helper; production runtime is copied without edits."""
from pathlib import Path as _BPath
import hashlib as _BHash
import json as _BJson
import os as _BOs
import sys as _BSys
import shutil as _BShutil
import uuid as _BUuid
import zipfile as _BZip
from datetime import datetime as _BDatetime

_B_ROOT = '/home/xilinx/jupyter_notebooks/User/Overlays/OV5640_UI/mask_rtl_1010_v1'
_B_ZIP = '1101_scan3d_board_payload_v1.zip'
_B_ZIP_SHA = '5d1e48718e0056715d626c938fa5e0b6788f8e4d17803339b9f4163151004c2f'
_B_ZIP_BYTES = 963145
_B_MANIFEST = '1101_board_payload_manifest_v1.json'


def _b_sha(path):
    digest = _BHash.sha256()
    with _BPath(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _b_save(path, value):
    _BPath(path).write_text(_BJson.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def _b_compare(root, manifest):
    checks = []
    for item in manifest['files']:
        path = root / item['name']
        actual = _b_sha(path) if path.is_file() else None
        checks.append(dict(file=item['name'], expected_sha256=item['sha256'],
                           actual_sha256=actual, matches=actual == item['sha256']))
    return checks


def _b_find_package(root, expected_sha, package_path=None):
    if package_path is not None:
        path = _BPath(package_path).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError('指定ZIP不存在：' + str(path))
        if _b_sha(path) != expected_sha:
            raise RuntimeError('指定ZIP的SHA不匹配，未安装。')
        return path
    directories = []
    for directory in [root, _BPath.cwd(), _BPath('/home/xilinx/jupyter_notebooks')]:
        directory = directory.resolve()
        if directory.is_dir() and directory not in directories:
            directories.append(directory)
    candidates = []
    observed = []
    for directory in directories:
        preferred = directory / _B_ZIP
        names = ([preferred] if preferred.is_file() else [])
        names += [path for path in sorted(directory.iterdir()) if path != preferred and path.is_file()
                  and (path.name.lower().startswith('1101_scan3d_board_payload')
                       or path.stat().st_size == _B_ZIP_BYTES)]
        for path in names:
            observed.append(str(path))
            if path.stat().st_size == _B_ZIP_BYTES and _b_sha(path) == expected_sha:
                candidates.append(path)
    if candidates:
        return candidates[0]
    raise RuntimeError('没有找到SHA匹配的验收ZIP，尚未备份或覆盖运行文件。\n'
        '当前内核目录：%s\n安装目标：%s\n查找过：%s\n发现的候选文件：%s\n'
        '请将原ZIP上传到助手所在目录，文件不必手动解压；保留这段输出。' %
        (_BPath.cwd(), root, ', '.join(str(path) for path in directories), ', '.join(observed) or '无'))


def _b_install(root=None, expected_zip_sha=None, fixture=False, package_path=None):
    root = _BPath(root or _B_ROOT).resolve()
    if not fixture and not _BSys.platform.startswith('linux'):
        raise RuntimeError('此格只在PYNQ运行；不要在Windows运行。')
    if not root.is_dir():
        raise RuntimeError('原工程安装目录不存在：%s\n当前内核目录：%s\n未安装，请保留这两行路径。' % (root, _BPath.cwd()))
    package = _b_find_package(root, expected_zip_sha or _B_ZIP_SHA, package_path)
    print('已找到并核对ZIP：', package)
    print('运行文件安装目标：', root)
    with _BZip.ZipFile(str(package), 'r') as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError('ZIP损坏：' + bad)
        manifest = _BJson.loads(archive.read(_B_MANIFEST).decode('utf8'))
        expected_names = {item['name'] for item in manifest['files']}
        if len(manifest['files']) != 40 or len(expected_names) != 40 or set(archive.namelist()) != expected_names | {_B_MANIFEST}:
            raise RuntimeError('ZIP文件清单异常，未安装。')
        if any(_BPath(name).name != name or '/' in name or '\\' in name or name in ('.', '..') for name in expected_names):
            raise RuntimeError('ZIP包含目录或越界路径，未安装。')
        loaded = [name[:-3] for name in expected_names if name.endswith('.py') and name[:-3] in _BSys.modules]
        if loaded and not fixture:
            raise RuntimeError('当前内核已加载扫描模块，请重启本Notebook内核后从第1格执行：' + ', '.join(loaded))
        for item in manifest['files']:
            raw = archive.read(item['name'])
            if len(raw) != item['bytes'] or _BHash.sha256(raw).hexdigest() != item['sha256']:
                raise RuntimeError('ZIP载荷校验失败：' + item['name'])
        old_size = sum((root / name).stat().st_size for name in expected_names if (root / name).is_file())
        new_size = sum(item['bytes'] for item in manifest['files'])
        if _BShutil.disk_usage(str(root)).free < old_size + new_size + 32 * 1024 * 1024:
            raise RuntimeError('SD卡空间不足，未安装；请先保留已有数据并释放空间。')
        session = root / ('1101_board_acceptance_' + _BDatetime.utcnow().strftime('%Y%m%d_%H%M%S') + '_' + _BUuid.uuid4().hex[:6])
        session.mkdir(exist_ok=False)
        backup = session / 'previous_files'
        backup.mkdir()
        for name in sorted(expected_names | {_B_MANIFEST}):
            path = root / name
            if path.exists():
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError('目标不是普通文件，停止：' + str(path))
                _BShutil.copy2(str(path), str(backup / name))
        print('原同名文件备份目录：', backup, flush=True)
        # No extraction paths are trusted; only the verified flat names are written.
        for name in sorted(expected_names):
            (root / name).write_bytes(archive.read(name))
        _b_save(root / _B_MANIFEST, manifest)
    checks = _b_compare(root, manifest)
    if not all(item['matches'] for item in checks):
        raise RuntimeError('写入后核对失败；原文件备份在：' + str(backup))
    state = dict(root=str(root), session=str(session), manifest=manifest,
                 package_source=str(package), upload_directory=str(package.parent),
                 fixture=fixture, installed_at_utc=_BDatetime.utcnow().isoformat() + 'Z')
    _b_save(session / 'installation.json', dict(root=str(root), backup=str(backup),
        uploaded_zip_sha256=_b_sha(package), package_source=str(package), files=checks, fixture=fixture,
        public_source_manifest_sha256=manifest['public_source_manifest_sha256']))
    print('已安装并逐项核对40个配套文件。原同名文件备份：', backup)
    print('本次验收编号：', session.name)
    print('公开源清单SHA前12位：', manifest['public_source_manifest_sha256'][:12])
    print('下一步运行第2格启动触屏流程；不要另外打开旧扫描Notebook。')
    return state


def _b_run(state):
    if state.get('fixture'):
        raise RuntimeError('本地夹具不能加载硬件。')
    import contextlib
    import importlib
    import traceback
    root = _BPath(state['root'])
    checks = _b_compare(root, state['manifest'])
    if not all(item['matches'] for item in checks):
        raise RuntimeError('运行文件已被改动，停止；重新核对安装包。')
    session = _BPath(state['session'])
    state['flow_scope'] = {}
    try:
        state['boot_id_at_start'] = _BPath('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        state['boot_id_at_start'] = None
    _b_save(session / 'runtime_before.json', checks)
    old_stdout = _BSys.stdout
    class Tee:
        def __init__(self, stream):
            self.stream = stream
        def write(self, value):
            old_stdout.write(value)
            self.stream.write(value)
            if '\n' in value:
                self.stream.flush()
            return len(value)
        def flush(self):
            old_stdout.flush()
            self.stream.flush()
    with (session / 'notebook_console.txt').open('w', encoding='utf8') as log:
        with contextlib.redirect_stdout(Tee(log)):
            try:
                _BSys.path.insert(0, str(root))
                module = importlib.import_module('touch_scan')
                if _BPath(module.__file__).resolve().parent != root.resolve():
                    raise RuntimeError('touch_scan加载路径错误：' + module.__file__)
                state['flow_result'] = module.run(state['flow_scope'], root / 'scan_workflow.ipynb')
                state['flow_returned_normally'] = True
            except BaseException as error:
                state['flow_error'] = type(error).__name__ + ': ' + str(error)
                log.write(traceback.format_exc())
                raise
            finally:
                scope = state['flow_scope'].get('touch_flow_scope') or state.get('flow_result') or {}
                run = scope.get('RUN_DIR')
                state['run_dir'] = str(run) if run is not None else None
                _b_save(session / 'flow_exit.json', dict(
                    run_dir=state['run_dir'], returned_normally=state.get('flow_returned_normally', False),
                    error=state.get('flow_error'), at_utc=_BDatetime.utcnow().isoformat() + 'Z'))
    print('触屏流程已退出。请先保存Notebook，再运行第3格归档本轮资料。')


def _b_export(state, operator_checks, fixture_info=None):
    root = _BPath(state['root']).resolve()
    session = _BPath(state['session']).resolve()
    if root not in session.parents:
        raise RuntimeError('验收会话目录越界。')
    runtime_checks = _b_compare(root, state['manifest'])
    scope = state.get('flow_scope', {}).get('touch_flow_scope') or state.get('flow_result') or {}
    run_text = state.get('run_dir') or scope.get('RUN_DIR')
    run = _BPath(run_text).resolve() if run_text else None
    if run is not None and (root not in run.parents or not run.is_dir()):
        raise RuntimeError('本轮扫描目录无效；不自动选取其他历史目录。')
    info_dir = session / ('board_info_' + _BUuid.uuid4().hex[:6])
    _b_collect_info(root, info_dir, fixture_info)
    required_root = ['1010_no_turntable_v1.npy', '1010_empty_turntable_v1.npy',
        '1010_calibration_v1.json', '1014_color24_v1.npz', '1014_scan24_color_masks_v1.npz',
        '1028_display_clock_v98.json', '1030_touch_flow_v103.json']
    missing = [name for name in required_root if run is None or not (run / name).is_file()]
    result_path = scope.get('result_dir')
    results = []
    if result_path:
        candidate = _BPath(result_path).resolve()
        if run is not None and run in candidate.parents:
            results = [candidate / 'result.json']
    elif run is not None:
        # Only inspect children of the exact run recorded by this same session.
        results = sorted(run.glob('1014_photo_guided_result_*/result.json'))
    results = [path for path in results if path.is_file()]
    report = {}
    if len(results) != 1:
        missing.append('unique reconstruction result.json')
    else:
        report = _BJson.loads(results[0].read_text(encoding='utf8'))
        mesh = report.get('detail_mesh_file')
        for name in ['occupancy.npy', 'run_log.txt'] + ([mesh] if mesh else []):
            if not (results[0].parent / name).is_file():
                missing.append('reconstruction/' + name)
        if not mesh:
            missing.append('main detail_mesh_file')
    events = []
    if run is not None and (run / '1030_touch_flow_v103.json').is_file():
        events = _BJson.loads((run / '1030_touch_flow_v103.json').read_text(encoding='utf8')).get('events', [])
    completed8 = any(row.get('event') == 'completed' and row.get('step') == 8 for row in events)
    checks = dict(operator_checks)
    needed_checks = ['camera_clear_color', 'screen_aligned', 'chinese_text_legible',
                     'touch_step_buttons', 'model_rotation', 'model_zoom', 'volume_visible']
    if set(checks) != set(needed_checks) or any(value is not None and not isinstance(value, bool) for value in checks.values()):
        raise ValueError('人工检查字段不完整，或值不是True/False/None。')
    environment = _BJson.loads((info_dir / 'board_environment.json').read_text(encoding='utf8'))
    ddr = _BJson.loads((info_dir / 'ddr_reservation.json').read_text(encoding='utf8'))
    clock = _BJson.loads((info_dir / 'clock_status.json').read_text(encoding='utf8'))
    initial_clock = {}
    if run is not None and (run / '1028_display_clock_v98.json').is_file():
        try:
            initial_clock = _BJson.loads((run / '1028_display_clock_v98.json').read_text(encoding='utf8'))
        except (ValueError, OSError):
            missing.append('readable initialization clock JSON')
    boot_start = state.get('boot_id_at_start')
    boot_now = environment.get('boot_id')
    record = dict(test_fixture=bool(state.get('fixture')), run_dir=str(run) if run else None,
        public_source_manifest_sha256=state['manifest']['public_source_manifest_sha256'],
        expected_payload=state['manifest'], runtime_files=runtime_checks, operator_checks=checks,
        missing_expected_files=missing, step8_completed_event=completed8,
        all_operator_checks_confirmed=all(checks.get(key) is True for key in needed_checks),
        expected_files_unchanged=all(row['matches'] for row in runtime_checks),
        initialization_clock_passed=initial_clock.get('passed') is True,
        export_fclk0_mhz=clock.get('fclk0_register_mhz'),
        export_clock_matches_expected=clock.get('within_expected_tolerance') is True,
        ddr_reservation_confirmed=ddr.get('reservation_confirmed') is True,
        result_volume_cm3=report.get('volume_cm3'),
        shape_consistency_passed=report.get('shape_consistency_passed'),
        physical_accuracy_verified=report.get('physical_accuracy_verified'),
        boot_id_at_start=boot_start, boot_id_at_export=boot_now,
        result_scope='technical board acceptance; physical accuracy and paired performance require separate experiments')
    record['review_status'] = ('ready_for_evidence_review' if not missing and completed8
        and record['all_operator_checks_confirmed'] and record['expected_files_unchanged']
        and record['initialization_clock_passed'] and record['export_clock_matches_expected']
        and record['ddr_reservation_confirmed']
        and boot_start == boot_now and boot_now is not None and not state.get('fixture') else 'needs_review')
    _b_save(session / 'acceptance_summary.json', record)
    _b_save(session / 'runtime_after.json', runtime_checks)
    items = []
    if run is not None:
        items.extend((path, 'scan_run/' + path.relative_to(run).as_posix())
                     for path in sorted(run.rglob('*')) if path.is_file() and not path.is_symlink()
                     and path.suffix != '.tmp' and '.building.' not in path.name)
    for path in sorted(session.rglob('*')):
        if path.is_file() and 'previous_files' not in path.relative_to(session).parts:
            items.append((path, 'acceptance/' + path.relative_to(session).as_posix()))
    for item in state['manifest']['files']:
        items.append((root / item['name'], 'runtime_used/' + item['name']))
    items.append((root / _B_MANIFEST, 'runtime_used/' + _B_MANIFEST))
    for path in sorted(root.glob('*.so')):
        if path.is_file() and not path.is_symlink():
            items.append((path, 'native_cache/' + path.name))
    touch_calibration = root / '1026_touch_calibration_v87.json'
    if touch_calibration.is_file():
        items.append((touch_calibration, 'board_config/' + touch_calibration.name))
    helper_name = state.get('helper_notebook_name', '1101_board_acceptance_v1.ipynb')
    helper_paths = []
    for directory in [_BPath(state.get('upload_directory', str(root))), _BPath.cwd(), root]:
        if directory.is_dir():
            for path in sorted(directory.glob(_BPath(helper_name).stem + '*.ipynb')):
                if path.is_file() and path.resolve() not in helper_paths:
                    helper_paths.append(path.resolve())
    for index, path in enumerate(helper_paths):
        items.append((path, 'acceptance/helper_notebooks/%d_%s' % (index + 1, path.name)))
    required_space = sum(path.stat().st_size for path, name in items) + 32 * 1024 * 1024
    if _BShutil.disk_usage(str(root)).free < required_space:
        raise RuntimeError('SD卡空间不足，请先保留会话和扫描目录，勿删除原数据；本次未生成ZIP。')
    target = root / ('1101_board_acceptance_evidence_' + _BDatetime.utcnow().strftime('%Y%m%d_%H%M%S') + '_' + _BUuid.uuid4().hex[:6] + '.zip')
    hashes = []
    try:
        with _BZip.ZipFile(str(target), 'x', compression=_BZip.ZIP_STORED, allowZip64=True) as package:
            for index, (path, name) in enumerate(items):
                info = _BZip.ZipInfo.from_file(str(path), arcname=name)
                digest = _BHash.sha256()
                count = 0
                with path.open('rb') as source:
                    with package.open(info, 'w', force_zip64=True) as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b''):
                            output.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                hashes.append(dict(file=name, bytes=count, sha256=digest.hexdigest()))
                if index % 20 == 0:
                    print('正在归档 %d/%d' % (index + 1, len(items)), flush=True)
            package.writestr('exported_files.json', _BJson.dumps(hashes, ensure_ascii=False, indent=2).encode('utf8'))
    except BaseException:
        if target.exists():
            target.rename(target.with_name(target.stem + '_incomplete.zip'))
        raise
    print('请下载这一个ZIP：', target.name)
    print('ZIP所在目录：', root)
    print('本次状态：', record['review_status'], '；人工未确认项：', [key for key in needed_checks if checks[key] is not True])
    print('请另保存触屏操作视频。ZIP包含本轮扫描、模型、环境和实际使用的40文件，不含旧文件备份或其他扫描。')
    try:
        from IPython.display import display, FileLink
        display(FileLink(_BOs.path.relpath(str(target), str(_BPath.cwd()))))
    except ImportError:
        pass
    state['archive'] = str(target)
    return str(target)

def _b_collect_info(_runtime, _directory, _fixture=None):
    from pathlib import Path
    from datetime import datetime
    import hashlib,importlib,json,math,os,platform,re,shutil,subprocess,sys,uuid,zipfile

    runtime=Path(_runtime)
    proc=Path('/proc');dt=Path('/proc/device-tree')
    if _fixture is not None:
        runtime=Path(_fixture['runtime']);proc=Path(_fixture['proc']);dt=Path(_fixture['dt'])
    elif not sys.platform.startswith('linux'):
        raise RuntimeError('请把本Notebook上传到PYNQ后运行，不在Windows电脑上运行。')
    if not runtime.is_dir():
        raise RuntimeError('板上工程目录不存在：%s；没有加载硬件或修改配置。'%runtime)
    errors=[]
    def read(path,binary=False):
        try:return path.read_bytes() if binary else path.read_text(encoding='utf8',errors='replace')
        except OSError as error:
            errors.append(dict(path=str(path),error=str(error)));return None
    boot=read(proc/'sys/kernel/random/boot_id')
    boot=boot.strip() if boot else None
    stamp=datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    directory=Path(_directory)
    directory.mkdir(exist_ok=False)
    base=dict(recorded_at_utc=datetime.utcnow().isoformat()+'Z',boot_id=boot,
              test_fixture=_fixture is not None,exporter_loaded_bit=False,exporter_captured_camera=False,
              exporter_wrote_clock_registers=False,existing_scan_files_modified=False)
    versions={}
    for name in ('pynq','numpy','PIL','scipy'):
        try:
            if _fixture is not None:
                versions[name]=dict(version='fixture',path=None)
            else:
                module=importlib.import_module(name)
                versions[name]=dict(version=str(getattr(module,'__version__','unknown')),
                                    path=getattr(module,'__file__',None))
        except Exception as error:versions[name]=dict(error=type(error).__name__+': '+str(error))
    hashes={};hash_errors=[]
    for path in sorted(runtime.iterdir()):
        if not path.is_file():continue
        if path.suffix not in ('.py','.c','.ipynb','.bit','.hwh','.bin'):continue
        try:
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                while True:
                    chunk=stream.read(1024*1024)
                    if not chunk:break
                    digest.update(chunk)
            hashes[path.name]=digest.hexdigest()
        except OSError as error:hash_errors.append(dict(file=path.name,error=str(error)))
    gcc=None
    if _fixture is None:
        compiler=shutil.which('gcc')
        if compiler:
            try:
                result=subprocess.run([compiler,'--version'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=8)
                gcc=result.stdout.decode('utf8','replace').splitlines()[0]
            except Exception as error:gcc=type(error).__name__+': '+str(error)
    loaded={}
    for name in ('scan_pipeline','pl_mask','subject_region','subject_evidence','scan_screen','touch_scan'):
        module=sys.modules.get(name)
        if module is not None:loaded[name]=str(getattr(module,'__file__','unknown'))
    environment=dict(base,python=sys.version,executable=sys.executable,platform=platform.platform(),
        machine=platform.machine(),modules=versions,gcc=gcc,runtime_directory=str(runtime),
        runtime_sha256=hashes,source_hash_errors=hash_errors,loaded_module_paths=loaded,
        note='Current environment and on-disk source hashes; does not prove which version made a historical scan.')

    start=0x01800000;end=0x018bb7ff
    reserved=dt/'reserved-memory';ranges=[];cover=[]
    def cells(name):
        path=reserved/name
        if not path.is_file():path=dt/name
        data=read(path,True) if path.is_file() else None
        return int.from_bytes(data,'big') if data else 1
    ac=cells('#address-cells');sc=cells('#size-cells')
    if reserved.is_dir() and ac in (1,2) and sc in (1,2):
        for child in sorted(reserved.iterdir()):
            if not child.is_dir() or not (child/'reg').is_file():continue
            data=read(child/'reg',True)
            if data is None:continue
            status_raw=read(child/'status',True) if (child/'status').is_file() else b'okay'
            status=status_raw.decode('ascii','replace').strip('\x00') if status_raw is not None else 'unreadable'
            enabled=status in ('okay','ok','')
            stride=4*(ac+sc)
            if len(data)%stride:
                errors.append(dict(path=str(child/'reg'),error='incomplete reg tuple'));continue
            for pos in range(0,len(data),stride):
                lo=int.from_bytes(data[pos:pos+4*ac],'big')
                size=int.from_bytes(data[pos+4*ac:pos+stride],'big')
                hi=lo+size-1
                reusable=(child/'reusable').exists()
                record=dict(node=child.name,start=hex(lo),end=hex(hi),size_bytes=size,
                    status=status,no_map=(child/'no-map').exists(),reg_hex=data.hex(),
                    reusable=reusable,covers_framebuffer=bool(size>0 and enabled and not reusable and lo<=start and hi>=end))
                ranges.append(record)
                if record['covers_framebuffer']:cover.append(record)
    iomem=read(proc/'iomem');iomem=iomem if iomem is not None else ''
    reserved_lines=[]
    for line in iomem.splitlines():
        match=re.match(r'\s*([0-9a-fA-F]+)-([0-9a-fA-F]+)\s*:\s*(.*)',line)
        if match and 'reserved' in match.group(3).lower():
            lo,hi=int(match.group(1),16),int(match.group(2),16)
            if lo<=start and hi>=end:reserved_lines.append(line)
    evidence='device_tree_covering_range' if cover else 'iomem_covering_reserved_range' if reserved_lines else 'not_confirmed'
    ddr=dict(base,framebuffer_start=hex(start),framebuffer_end=hex(end),size_bytes=end-start+1,
        reservation_evidence=evidence,reservation_confirmed=bool(cover or reserved_lines),
        device_tree_root=str(dt),address_cells=ac,size_cells=sc,ranges=ranges,
        iomem_matching_reserved_lines=reserved_lines,read_errors=errors,
        note='Read-only evidence for this boot. Does not replace the scan initialization guard or prove a prior boot.')

    clock=dict(base,read_ok=False,expected_fclk0_mhz=50.0,
        note='Current PS clock-register-derived frequency, not an oscilloscope measurement or proof of an earlier scan.')
    try:
        if _fixture is not None:value=float(_fixture['fclk0_mhz'])
        else:
            from pynq import Clocks
            value=float(Clocks.fclk0_mhz)
        if not math.isfinite(value) or value<=0:raise ValueError('invalid frequency')
        clock.update(read_ok=True,fclk0_register_mhz=value,within_expected_tolerance=abs(value-50.0)<=.05)
    except Exception as error:clock['error']=type(error).__name__+': '+str(error)
    for name,record in [('board_environment.json',environment),('ddr_reservation.json',ddr),('clock_status.json',clock)]:
        (directory/name).write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    (directory/'proc_iomem.txt').write_text(iomem,encoding='utf8')
    return str(directory)
