"""Run the delivered RTL functional tests with AMD XSim, no board access."""
from pathlib import Path
import argparse
import json
import os
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--vivado-bin',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();out=Path(args.output).resolve();bin_dir=Path(args.vivado_bin).resolve()
    if out.exists():parser.error('output exists; choose a fresh directory')
    if out==ROOT or ROOT in out.parents:parser.error('output must be outside repository')
    suffix='.bat' if os.name=='nt' else ''
    commands={name:bin_dir/(name+suffix) for name in ('xvlog','xelab','xsim')}
    for path in commands.values():
        if not path.is_file():parser.error('tool missing: '+str(path))
    out.mkdir(parents=True)
    src=ROOT/'hardware/vivado/ov5640_ddr3_tft_hdmi.srcs/sources_1/new'
    rtl=[src/(name+'.v') for name in ('gt911_reader','touch_i2c','touch_viewer_ctrl','viewer_frame_reader','ui_mixer','display_stream_probe')]
    tests=[('1026_tb_touch_v87.sv','tb_touch'),('1026_tb_absent_v87.sv','tb_absent'),
        ('1026_tb_color_v87.sv','tb_color'),('1026_tb_frame_v87.sv','tb_frame'),
        ('1026_tb_registers_v87.sv','tb_registers'),('1027_tb_probe_regs_v90.sv','tb_probe_regs_v90'),
        ('1027_tb_probe_v90.sv','tb_probe_v90'),('1028_tb_display_probe_v99.sv','tb_display_probe_v99'),
        ('1028_tb_display_registers_v99.sv','tb_display_registers_v99')]
    def run(tool,args,log):
        r=subprocess.run([str(commands[tool])]+[str(a) for a in args],cwd=str(out),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300)
        (out/log).write_bytes(r.stdout)
        text=r.stdout.decode('ascii','backslashreplace')
        if r.returncode or re.search(r'(^|\n)\s*(?:ERROR:|FATAL_ERROR:|Fatal:)',text):raise RuntimeError(log+' failed; inspect log')
        return text
    run('xvlog',['--sv']+rtl+[ROOT/'sim/rtl'/file for file,_ in tests],'compile.log')
    records=[]
    for file,top in tests:
        snapshot='check_'+top
        run('xelab',['-mt','off',top,'-s',snapshot],top+'_elaborate.log')
        text=run('xsim',[snapshot,'-R'],top+'_run.log')
        passed=bool(re.search(r'PASS\b',text)) and not re.search(r'\bFatal\b|\$fatal|FATAL_ERROR:',text)
        records.append(dict(top=top,source=file,passed=passed,run_log=top+'_run.log'))
        print(top,'PASS' if passed else 'FAIL',flush=True)
    record=dict(passed=all(r['passed'] for r in records),tests=records,board_tested=False,
                log_scope='external_output_directory',log_base='--output',source_base='sim/rtl',
                camera_full_frame_test_run=False,camera_full_frame_note='Requires generated FIFO netlist; reference results are archived separately.')
    (out/'rtl_summary.json').write_text(json.dumps(record,indent=2),encoding='utf8')
    return 0 if record['passed'] else 1


if __name__=='__main__':sys.exit(main())
