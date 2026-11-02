"""Deterministic RTL vectors. Synthetic cases are not physical measurements."""
from pathlib import Path
import argparse,hashlib,json,sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/runtime'))
import ps_demo
import pl_mask


def hexfile(path,values):
    values=np.asarray(values,dtype=np.int64).ravel()
    path.write_text(''.join('%08x\n'%(int(v)&0xffffffff) for v in values),encoding='ascii')


def reconstruction(directory,shift=0):
    folder=directory/'mem';folder.mkdir(parents=True,exist_ok=True)
    n=32;mw=mh=128;nv=24
    y,x=np.mgrid[:mh,:mw]
    masks=np.stack([((x>=mw//2-10+i%2)&(x<=mw//2+10-i%2)&(y>=mh-26)&(y<=mh-4)&~((x<mw//2-5)&(y<mh-21+i%3))) for i in range(nv)])
    angles=np.arange(nv)*360./nv
    kv,ofv,kux,kuy,ofu=ps_demo.plan_geometry(angles,n,mw,mh,.75,1.,axis_col=(mw-1)/2.-.2,v_ground=mh-3.8)
    ofu=ofu.copy();ofu[1]-=shift*65536
    if shift:masks[1]=True
    coef=[kv,ofv]
    for i in range(nv):coef.extend([kux[i],kuy[i],ofu[i]])
    # Scalar oracle evaluates the public affine equations independently of RTL's accumulators.
    occupied=[]
    for z in range(n):
        v=(int(ofv)-z*int(kv))//65536
        for yy in range(n):
            for xx in range(n):
                ok=True
                for i in range(nv):
                    u=(int(ofu[i])+xx*int(kux[i])+yy*int(kuy[i]))//65536
                    if not (0<=u<mw and 0<=v<mh and masks[i,v,u]):ok=False;break
                occupied.append(ok)
    words=np.zeros(n**3//32,np.uint32)
    for i,value in enumerate(occupied):
        if value:words[i//32]|=np.uint32(1)<<np.uint32(i%32)
    assert 0<sum(occupied)<n**3
    hexfile(folder/'mask.mem',ps_demo.pack_masks(~masks))
    hexfile(folder/'coef_hex.mem',coef);hexfile(folder/'occ_expected.mem',words)
    text=''.join('`define RECON_%s %d\n'%(key,value) for key,value in
        [('N',n),('MW',mw),('MH',mh),('NV',nv),('MWORDS',nv*mh*mw//32),('OWORDS',n**3//32)])
    (folder/'recon_cfg.vh').write_text(text,encoding='ascii')
    return dict(n=n,mw=mw,mh=mh,nv=nv,occupied_voxels=int(sum(occupied)),shift_px=shift,
                coefficient_source='PS plan_geometry; scalar affine oracle; not physical validation')


def photo(directory,random=False):
    directory.mkdir(parents=True,exist_ok=True)
    if random:
        rng=np.random.RandomState(1901)
        frame=rng.randint(0,65536,pl_mask.SHAPE).astype(np.uint16)
        ref=rng.randint(0,65536,pl_mask.SHAPE).astype(np.uint16)
        cfg=dict(lut=rng.randint(0,256,(3,256)).astype(np.uint8),
            inverse_variance=rng.randint(1,65536,(3,16)).astype(np.uint16),low=0x20000000,high=0xc0000000)
    else:
        yy,xx=np.mgrid[:pl_mask.SHAPE[0],:pl_mask.SHAPE[1]]
        ref=(((xx%32)<<11)|((yy%64)<<5)|((xx+yy)%32)).astype(np.uint16)
        frame=ref.copy()
        frame[100:360,200:550]^=np.uint16(0xf81f)
        cfg=dict(lut=np.tile(np.arange(256,dtype=np.uint8),(3,1)),
            inverse_variance=np.full((3,16),4096,dtype=np.uint16),
            low=1<<26,high=1<<28)
    strong,possible=pl_mask.reference_decisions(frame,ref,cfg)
    inputs=(ref.astype(np.uint32)<<16)|frame
    codes=strong.astype(np.uint32)+2*possible.astype(np.uint32)
    packed=np.bitwise_or.reduce(codes.reshape(-1,16)<<(2*np.arange(16,dtype=np.uint32)),axis=1)
    for name,values in [('input',inputs),('expected',packed),('lut',cfg['lut']),('noise',cfg['inverse_variance']),('thresholds',[cfg['high'],cfg['low']])]:
        hexfile(directory/(name+'.mem'),values)
    return dict(pixels=int(frame.size),random_stress=random,strong=int(strong.sum()),possible=int(possible.sum()))


def generate(out):
    out.mkdir(parents=True,exist_ok=True)
    report=dict(recon=reconstruction(out/'recon'),recon_shift=reconstruction(out/'recon_shift',64),
                photo_pattern=photo(out/'photo_pattern'),photo_random=photo(out/'photo_random',True),board_tested=False)
    (out/'vectors.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    print(json.dumps(generate(Path(args.output)),indent=2))
