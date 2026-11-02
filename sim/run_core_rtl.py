"""Execute shipped PL segmentation and carving RTL, not a software replacement."""
from pathlib import Path
import argparse,json,os,re,subprocess,sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from generate_core_vectors import generate

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--vivado-bin',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    out=Path(args.output).resolve();bin_dir=Path(args.vivado_bin).resolve()
    if out.exists() or ROOT==out or ROOT in out.parents:p.error('choose a fresh output directory outside repository')
    out.mkdir(parents=True);vectors=generate(out/'vectors')
    suffix='.bat' if os.name=='nt' else ''
    def run(name,arguments,folder,log):
        tool=bin_dir/(name+suffix)
        r=subprocess.run([str(tool)]+[str(a) for a in arguments],cwd=str(folder),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
        (out/log).write_bytes(r.stdout)
        text=r.stdout.decode('ascii','backslashreplace')
        if r.returncode or re.search(r'(^|\n)\s*(ERROR:|Fatal:|FATAL_ERROR:)',text):raise RuntimeError(log+' failed')
        return text
    src=ROOT/'hardware/vivado/ov5640_ddr3_tft_hdmi.srcs/sources_1/new'
    recon=out/'vectors/recon'
    run('xvlog',['--sv','-i',str(recon),src/'recon_axil.v',src/'voxel_carver.v',src/'recon_top.v',ROOT/'hardware/tests/tb_recon.v'],recon,'recon_compile.log')
    run('xelab',['-mt','off','tb_recon','-s','recon_check'],recon,'recon_elaborate.log')
    results=[]
    for name in ('recon','recon_shift'):
        # Keep the compiled snapshot; replace input vectors only in this external test workspace.
        if name=='recon_shift':
            for source in (out/'vectors/recon_shift/mem').glob('*'):(recon/'mem'/source.name).write_bytes(source.read_bytes())
        text=run('xsim',['recon_check','-R'],recon,name+'_run.log')
        ok='PASS' in text and not re.search(r'\bFAIL\b|!!|Fatal:',text)
        results.append(dict(name=name,passed=ok,run_log=name+'_run.log',scope='actual recon_top AXI transfers and all occupancy words'))
    photo=out/'photo_sim';photo.mkdir()
    run('xvlog',['--sv',ROOT/'hardware/rtl/1016_photo_mask_axis_v19.sv',ROOT/'sim/rtl/tb_photo_mask.sv'],photo,'photo_compile.log')
    run('xelab',['-mt','off','tb_photo_mask','-s','photo_check'],photo,'photo_elaborate.log')
    for name in ('photo_protocol','photo_pattern','photo_random'):
        arguments=['photo_check','-R']
        if name!='photo_protocol':
            relative=os.path.relpath(str(out/'vectors'/name),str(photo)).replace('\\','/')
            arguments+=['-testplusarg','VECTORS_'+relative]
        text=run('xsim',arguments,photo,name+'_run.log')
        ok='PASS' in text and not re.search(r'\bFAIL\b|Fatal:',text)
        results.append(dict(name=name,passed=ok,run_log=name+'_run.log',scope='actual photo_mask_axis full-frame decisions and AXI/stream handshake'))
    report=dict(passed=all(r['passed'] for r in results),cases=results,vectors=vectors,board_tested=False,
                physical_accuracy_validated=False,log_scope='external_output_directory',log_base='--output')
    (out/'core_rtl_checks.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,indent=2));return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
