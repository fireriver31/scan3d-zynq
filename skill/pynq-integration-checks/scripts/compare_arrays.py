"""Exact comparison of explicitly selected NPZ fields; no hardware access."""
import argparse,hashlib,json,sys
from pathlib import Path
import numpy as np

def compare(actual,reference,keys):
    report=dict(schema_version=1,comparison='exact_dtype_shape_values',passed=False,fields=[],errors=[],
                physical_accuracy_proven=False,reference_independence_proven=False)
    if not keys:report['errors'].append('at least one explicit field is required');return report
    try:
        with np.load(str(actual),allow_pickle=False) as a,np.load(str(reference),allow_pickle=False) as r:
            for key in dict.fromkeys(keys):
                if key not in a.files or key not in r.files:
                    report['errors'].append('missing selected field: '+key);continue
                x,y=a[key],r[key];field=dict(key=key,actual_shape=list(x.shape),reference_shape=list(y.shape),
                    actual_dtype=str(x.dtype),reference_dtype=str(y.dtype),passed=False)
                field['actual_sha256']=hashlib.sha256(np.ascontiguousarray(x).tobytes()).hexdigest()
                field['reference_sha256']=hashlib.sha256(np.ascontiguousarray(y).tobytes()).hexdigest()
                if x.shape!=y.shape:field['reason']='shape_mismatch'
                elif x.dtype!=y.dtype:field['reason']='dtype_mismatch'
                else:
                    mismatch=np.not_equal(x,y);count=int(np.count_nonzero(mismatch))
                    field.update(mismatched_elements=count,passed=count==0)
                    if count:
                        coordinate=tuple(int(v) for v in np.unravel_index(int(np.flatnonzero(mismatch)[0]),x.shape))
                        field.update(first_coordinate=list(coordinate),actual_first=str(x[coordinate]),reference_first=str(y[coordinate]))
                report['fields'].append(field)
    except (OSError,ValueError,TypeError,KeyError) as exc:report['errors'].append(str(exc))
    report['passed']=not report['errors'] and len(report['fields'])==len(set(keys)) and all(v['passed'] for v in report['fields'])
    return report

def main():
    p=argparse.ArgumentParser(description='精确比较实际输出与独立参考NPZ的指定字段')
    p.add_argument('actual');p.add_argument('reference');p.add_argument('--key',required=True,action='append')
    p.add_argument('--out',required=True);a=p.parse_args();out=Path(a.out)
    if out.resolve() in (Path(a.actual).resolve(),Path(a.reference).resolve()):p.error('output must not overwrite an input')
    result=compare(a.actual,a.reference,a.key);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('PASS' if result['passed'] else 'FAIL',out)
    return 0 if result['passed'] else 2
if __name__=='__main__':sys.exit(main())
