"""Bounded sparse background witnesses; no dense PS pixel classifier.

Native RGB565 code intervals, actual reference texture and two held-out patch
folds support graph markers. Compatibility is conditional, not physical proof.
"""
import numpy as np
VERSION=51
PERFORMANCE_VERSION=51

def rgb565(a):
    a=np.asarray(a,dtype=np.uint16);r,g,b=(a>>11)&31,(a>>5)&63,a&31
    return np.stack(((r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)),axis=-1).astype(float)


def sample(reference,y,x):
    y=np.asarray(y,float);x=np.asarray(x,float)
    iy=np.floor(y).astype(int);ix=np.floor(x).astype(int)
    fy=(y-iy)[...,None];fx=(x-ix)[...,None]
    return (rgb565(reference[np.clip(iy,0,479),np.clip(ix,0,799)])*(1-fy)*(1-fx)+
            rgb565(reference[np.clip(iy+1,0,479),np.clip(ix,0,799)])*fy*(1-fx)+
            rgb565(reference[np.clip(iy,0,479),np.clip(ix+1,0,799)])*(1-fy)*fx+
            rgb565(reference[np.clip(iy+1,0,479),np.clip(ix+1,0,799)])*fy*fx)


def _sample_rgb(reference,y,x):
    iy=np.floor(y).astype(int);ix=np.floor(x).astype(int)
    fy=(y-iy)[...,None];fx=(x-ix)[...,None]
    return (reference[np.clip(iy,0,479),np.clip(ix,0,799)]*(1-fy)*(1-fx)+
            reference[np.clip(iy+1,0,479),np.clip(ix,0,799)]*fy*(1-fx)+
            reference[np.clip(iy,0,479),np.clip(ix+1,0,799)]*(1-fy)*fx+
            reference[np.clip(iy+1,0,479),np.clip(ix+1,0,799)]*fy*fx)



class _ReferenceWindow:
    """Decoded envelope of requested patches, indexed in original coordinates.

    No interpolation or quantization changes. In particular four tiny witness
    points no longer force a decode of 384,000 reference pixels on every call.
    """
    def __init__(self, reference, coords, radius):
        # Bilinear evaluation also reads the zero-weight +1 neighbour when
        # only the OTHER axis is fractional, so reserve radius+2, not +1.
        margin=int(radius)+2
        y0=max(0,int(coords[:,0].min())-margin)
        y1=min(480,int(coords[:,0].max())+margin+1)
        x0=max(0,int(coords[:,1].min())-margin)
        x1=min(800,int(coords[:,1].max())+margin+1)
        self.y0,self.x0=y0,x0
        self.rgb=rgb565(reference[y0:y1,x0:x1])
        self.pixels=(y1-y0)*(x1-x0)

    def __getitem__(self, index):
        y,x=index
        return self.rgb[y-self.y0,x-self.x0]


def _compatible_batch(frame,reference,lut,coords,noise,radius,fractional,stats,optimize=False):
    y,x=coords.T
    # This is exactly the existing first OR predicate, evaluated before
    # allocating/decoding each full patch rather than after it.
    tolerance=np.array([9.,5.,9.])[None,:]
    if noise is not None:tolerance=tolerance+2*np.asarray(noise[y,x],float)
    tolerance=np.broadcast_to(tolerance,(len(coords),3))
    if optimize:
        center_words=rgb565(frame[y,x]).astype(int)
        center_current=np.stack([lut[c,center_words[:,c]] for c in range(3)],-1)
        result=np.all(np.abs(center_current-reference[y,x])<=tolerance,axis=1)
        stats['center_matches']+=int(result.sum())
        if result.all():return result
    dy,dx=np.mgrid[-radius:radius+1,-radius:radius+1]
    yy=np.clip(y[:,None]+dy.ravel(),0,479);xx=np.clip(x[:,None]+dx.ravel(),0,799)
    words=rgb565(frame[yy,xx]).astype(int)
    current=np.stack([lut[c,words[...,c]] for c in range(3)],-1)
    if not optimize:
        center=(2*radius+1)**2//2
        result=np.all(np.abs(current[:,center]-reference[y,x])<=tolerance,axis=1)
        stats['center_matches']+=int(result.sum())
    phase=(dy.ravel()+dx.ravel())%2==0
    poses=(-1.,-.5,0.,.5,1.) if fractional else (-1,0,1)
    # Only the order of an OR over independent pose predicates changes.
    # Try zero and nearby displacements first; the set of tested poses is exact.
    pose_pairs=[(sy,sx) for sy in poses for sx in poses]
    if optimize:
        pose_pairs.sort(key=lambda pair:(pair[0]**2+pair[1]**2,pair[0],pair[1]))
    a=current-current.mean(1,keepdims=True);pa=np.sum(a*a,axis=(1,2))
    for sy,sx in pose_pairs:
        ids=np.flatnonzero(~result)
        if not len(ids):return result
        stats['pose_point_evaluations']+=len(ids)
        ref=(_sample_rgb(reference,yy[ids]+sy,xx[ids]+sx) if sy!=int(sy) or sx!=int(sx)
             else reference[np.clip(yy[ids]+int(sy),0,479),np.clip(xx[ids]+int(sx),0,799)])
        close=np.all(np.max(np.abs(current[ids]-ref),axis=1)<=tolerance[ids],axis=1)
        result[ids[close]]=True
        ids_left=np.flatnonzero(~close)
        if not len(ids_left):continue
        ref=ref[ids_left];ids=ids[ids_left]
        b=ref-ref.mean(1,keepdims=True);pb=np.sum(b*b,axis=(1,2))
        corr=np.sum(a[ids]*b,axis=(1,2))/np.maximum(np.sqrt(pa[ids]*pb),1e-9)
        informative=(pa[ids]>len(phase)*12)&(pb>len(phase)*12)&(corr>=.9)
        keep=np.flatnonzero(informative)
        if not len(keep):continue
        ref=ref[keep];ids=ids[keep]
        stats['heldout_point_evaluations']+=len(ids)
        valid=np.ones(len(ids),bool)
        for train in (phase,~phase):
            rc=ref[:,train];fc=current[ids][:,train]
            r=rc-rc.mean(1,keepdims=True);f=fc-fc.mean(1,keepdims=True)
            power=np.sum(r*r,axis=1);gain=np.sum(r*f,axis=1)/np.maximum(power,1.)
            useful=power>np.array([16.,4.,16.])[None,:]*train.sum()
            valid &= np.any(useful,axis=1)&np.all(~useful|((gain>.25)&(gain<4)),axis=1)
            gain=np.where(useful,gain,1.)
            prediction=(ref[:,~train]-rc.mean(1,keepdims=True))*gain[:,None,:]+fc.mean(1,keepdims=True)
            rms=np.sqrt(np.mean((current[ids][:,~train]-prediction)**2,axis=1))
            valid &= np.all(rms<=tolerance[ids],axis=1)
        result[ids[valid]]=True
    return result


def background_compatible(frame,reference,lut,coordinates,noise=None,radius=2,fractional=False,
                          progress=None,stats=None):
    """Same OR decision as v44; stop evaluating a point once it is true."""
    import time
    coords=np.asarray(coordinates,int)
    if stats is None:stats={}
    for key in ('points','center_matches','pose_point_evaluations','heldout_point_evaluations'):
        stats.setdefault(key,0)
    stats['points']+=len(coords)
    if not len(coords):return np.zeros(0,bool)
    # Decode once; these are lookup values, not PS foreground classifications.
    margin=int(radius)+2
    y0=max(0,int(coords[:,0].min())-margin);y1=min(480,int(coords[:,0].max())+margin+1)
    x0=max(0,int(coords[:,1].min())-margin);x1=min(800,int(coords[:,1].max())+margin+1)
    optimize=len(coords)<=128 and (y1-y0)*(x1-x0)<384000//4
    reference_rgb=_ReferenceWindow(reference,coords,radius) if optimize else rgb565(reference)
    decoded=reference_rgb.pixels if optimize else 384000
    stats['decoded_reference_pixels']=stats.get('decoded_reference_pixels',0)+decoded
    result=np.empty(len(coords),bool);last=time.monotonic()
    for start in range(0,len(coords),128):
        end=min(start+128,len(coords))
        result[start:end]=_compatible_batch(frame,reference_rgb,lut,coords[start:end],noise,radius,fractional,stats,optimize=optimize)
        now=time.monotonic()
        if progress is not None and (now-last>=1. or end==len(coords)):
            progress.update('背景纹理核验 %d/%d'%(end,len(coords)))
            last=now
    return result


def conditional_pairs(cx,ref_rgb,cur_rgb,compatible):
    """Exactly the old any-match relation, indexed by measured image column."""
    known=np.flatnonzero(compatible);paired=compatible.copy()
    if not len(known):return paired
    known=known[np.argsort(cx[known],kind='mergesort')];columns=cx[known]
    query=np.flatnonzero(~paired);query=query[np.argsort(cx[query],kind='mergesort')]
    for start in range(0,len(query),128):
        ids=query[start:start+128]
        lo=np.searchsorted(columns,cx[ids].min()-64,side='left')
        hi=np.searchsorted(columns,cx[ids].max()+64,side='right')
        neighbors=known[lo:hi]
        if not len(neighbors):continue
        match=np.abs(cx[ids,None]-cx[neighbors][None,:])<=64
        match &= np.all(np.abs(ref_rgb[ids,None]-ref_rgb[neighbors][None,:])<=np.array([9.,5.,9.]),axis=2)
        match &= np.all(np.abs(cur_rgb[ids,None]-cur_rgb[neighbors][None,:])<=np.array([9.,5.,9.]),axis=2)
        paired[ids]=match.any(1)
    return paired



def boundary_texture_loss(frame,reference,ground):
    """Detect a reference texture replaced by a flat material at ROI edges.

    It does not assert physical cropping. A changed boundary observation needs
    review even if a noisy PL threshold has erased the candidate altogether.
    Uniform/quantization-only references provide no such positive evidence.
    """
    bottom=int(np.floor(ground));y=np.r_[np.arange(2,bottom-2),np.arange(2,bottom-2)]
    x=np.r_[np.full(bottom-4,160),np.full(bottom-4,639)]
    dy,dx=np.mgrid[-1:2,-4:5];yy=y[:,None]+dy.ravel();xx=x[:,None]+dx.ravel()
    cur=rgb565(frame[yy,xx]);ref=rgb565(reference[yy,xx])
    cp=np.mean((cur-cur.mean(1,keepdims=True))**2,axis=(1,2))
    rp=np.mean((ref-ref.mean(1,keepdims=True))**2,axis=(1,2))
    # Test several consecutive pixel centers, not a single quantization edge.
    changed=(rp>16)&(cp<.5)&(np.max(np.abs(cur[:,13]-ref[:,13]),axis=1)>16)
    witnesses=[]
    for side in (0,1):
        ids=np.flatnonzero(changed&(np.arange(len(y))//max(bottom-4,1)==side))
        for k in ids:
            if k+2<len(y) and changed[k:k+3].all() and y[k+2]==y[k]+2:
                witnesses.append([int(x[k]),int(y[k])])
    return dict(requires_review=bool(witnesses),witness_pixels_uv=witnesses,
                source='raw_reference_texture_replaced_at_ROI_border',physical_crop_not_asserted=True)


