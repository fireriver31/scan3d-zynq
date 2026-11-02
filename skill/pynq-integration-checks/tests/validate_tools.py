from pathlib import Path
import argparse,ast,importlib.util,json,struct
import numpy as np
HERE=Path(__file__).resolve().parents[1]
def module(name):
    spec=importlib.util.spec_from_file_location(name,HERE/'scripts'/(name+'.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def main():
    p=argparse.ArgumentParser();p.add_argument('--workdir',required=True)
    p.add_argument('--actual-bit');p.add_argument('--actual-hwh');args=p.parse_args()
    work=Path(args.workdir);work.mkdir(parents=True,exist_ok=True)
    audit=module('overlay_audit');compare=module('compare_arrays').compare;tests=[]
    def check(name,ok):
        assert ok,name;tests.append(dict(test=name,passed=True))
    for script in (HERE/'scripts').glob('*.py'):ast.parse(script.read_text(encoding='utf8'),feature_version=(3,6))
    check('python36_syntax',True)
    original=np.arange(96,dtype=np.uint32).reshape(3,32)
    np.savez(work/'reference.npz',packed=original)
    np.savez(work/'actual.npz',packed=original.copy())
    x=original.copy();x[2,5]^=np.uint32(1<<17);np.savez(work/'bad.npz',packed=x)
    np.savez(work/'dtype.npz',packed=original.astype('i8'))
    np.savez(work/'shape.npz',packed=original.ravel())
    np.savez(work/'field.npz',other=original)
    check('equal_integer_arrays_pass',compare(work/'actual.npz',work/'reference.npz',['packed'])['passed'])
    r=compare(work/'bad.npz',work/'reference.npz',['packed'])
    check('single_bit_corruption_localized',not r['passed'] and r['fields'][0]['mismatched_elements']==1 and r['fields'][0]['first_coordinate']==[2,5])
    check('dtype_mismatch_detected',not compare(work/'dtype.npz',work/'reference.npz',['packed'])['passed'])
    check('shape_mismatch_detected',not compare(work/'shape.npz',work/'reference.npz',['packed'])['passed'])
    check('missing_requested_field_detected',not compare(work/'field.npz',work/'reference.npz',['packed'])['passed'])
    check('no_empty_field_list_false_pass',not compare(work/'actual.npz',work/'reference.npz',[])['passed'])
    magic=b'\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00'
    data=struct.pack('>H',len(magic))+magic+struct.pack('>H',1)
    for tag,value in [(b'a',b'demo\0'),(b'b',b'7z020clg400\0'),(b'c',b'2026/10/29\0'),(b'd',b'12:00:00\0')]:
        data+=tag+struct.pack('>H',len(value))+value
    data+=b'e'+struct.pack('>I',4)+b'\0\0\0\0'
    (work/'demo.bit').write_bytes(data)
    xml='<EDKSYSTEM VIVADOVERSION="test"><SYSTEMINFO DEVICE="7z020" PACKAGE="clg400"/><MODULE INSTANCE="demo_ip"/></EDKSYSTEM>'
    (work/'demo.hwh').write_text(xml,encoding='utf8')
    check('supported_metadata_fixture_passes',audit.audit(work/'demo.bit',work/'demo.hwh',['demo_ip'])['passed'])
    check('missing_expected_ip_detected',not audit.audit(work/'demo.bit',work/'demo.hwh',['missing_ip'])['passed'])
    (work/'wrong.hwh').write_text(xml,encoding='utf8')
    check('basename_mismatch_detected',not audit.audit(work/'demo.bit',work/'wrong.hwh')['passed'])
    (work/'demo.hwh').write_text(xml.replace('7z020','7z010'),encoding='utf8')
    check('part_mismatch_detected',not audit.audit(work/'demo.bit',work/'demo.hwh')['passed'])
    (work/'demo.hwh').write_text('<broken',encoding='utf8')
    check('invalid_xml_is_failure',not audit.audit(work/'demo.bit',work/'demo.hwh')['passed'])
    (work/'demo.hwh').write_text(xml,encoding='utf8');(work/'demo.bit').write_bytes(data[:-1])
    check('truncated_bit_payload_is_failure',not audit.audit(work/'demo.bit',work/'demo.hwh')['passed'])
    if args.actual_bit and args.actual_hwh:
        r=audit.audit(args.actual_bit,args.actual_hwh,['recon_0'])
        check('real_v99_metadata_pair_passes',r['passed'] and r['hwh_vivado_version']=='2026.1')
        (work/'actual_overlay_audit.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf8')
    report=dict(passed=len(tests),tests=tests,board_tested=False,hardware_writes_performed=False)
    (work/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
