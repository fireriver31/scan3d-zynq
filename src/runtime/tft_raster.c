/* Orthographic, double-sided triangle rasterizer for the physical TFT.
 * Display only: no writes to the source PLY, masks, occupancy or volume.
 */
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include <float.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif
API void tv_memory_barrier(void) { __sync_synchronize(); }
API int tv_render(const float *p, const int32_t *faces, const uint8_t *rgb,
                  int nv, int nf, float yaw, float pitch, float zoom,
                  uint16_t *out, int w, int h) {
    if(nv<=0 || nf<=0 || w<16 || h<16 || w>800 || h>480 ||
       !isfinite(yaw) || !isfinite(pitch) || !isfinite(zoom) || zoom<0.2f || zoom>5.f) return -1;
    float *q=(float*)malloc((size_t)nv*3*sizeof(float));
    float *z=(float*)malloc((size_t)w*h*sizeof(float));
    if(!q || !z){free(q);free(z);return -2;}
    float c=cosf(yaw),s=sinf(yaw),cp=cosf(pitch),sp=sinf(pitch);
    float scale=0.44f*(w<h?w:h)*zoom;
    for(int i=0;i<nv;i++) {
        float right=p[3*i]*c-p[3*i+1]*s;
        float depth=p[3*i]*s+p[3*i+1]*c;
        q[3*i]=w*.5f+right*scale;
        q[3*i+1]=h*.5f-(p[3*i+2]*cp-depth*sp)*scale;
        q[3*i+2]=depth*cp+p[3*i+2]*sp;
    }
    for(int i=0;i<w*h;i++){z[i]=-FLT_MAX;out[i]=0x1082;}
    for(int k=0;k<nf;k++) {
        int a=faces[3*k],b=faces[3*k+1],d=faces[3*k+2];
        if(a<0||b<0||d<0||a>=nv||b>=nv||d>=nv){free(q);free(z);return -3;}
        float x0=q[3*a],y0=q[3*a+1],x1=q[3*b],y1=q[3*b+1],x2=q[3*d],y2=q[3*d+1];
        float den=(y1-y2)*(x0-x2)+(x2-x1)*(y0-y2);
        if(fabsf(den)<1e-6f)continue;
        float l=fminf(x0,fminf(x1,x2)),r=fmaxf(x0,fmaxf(x1,x2));
        float t=fminf(y0,fminf(y1,y2)),bt=fmaxf(y0,fmaxf(y1,y2));
        if(r<0||l>=w||bt<0||t>=h)continue;
        int xmin=(int)fmaxf(0,floorf(l)),xmax=(int)fminf(w-1,ceilf(r));
        int ymin=(int)fmaxf(0,floorf(t)),ymax=(int)fminf(h-1,ceilf(bt));
        for(int y=ymin;y<=ymax;y++)for(int x=xmin;x<=xmax;x++) {
            float px=x+.5f,py=y+.5f;
            float u=((y1-y2)*(px-x2)+(x2-x1)*(py-y2))/den;
            float v=((y2-y0)*(px-x2)+(x0-x2)*(py-y2))/den,ww=1.f-u-v;
            if(u<-1e-5f||v<-1e-5f||ww<-1e-5f)continue;
            float depth=u*q[3*a+2]+v*q[3*b+2]+ww*q[3*d+2];
            int ix=y*w+x;
            if(depth>z[ix]) {
                z[ix]=depth;
                int rr=(int)(u*rgb[3*a]+v*rgb[3*b]+ww*rgb[3*d]+.5f);
                int gg=(int)(u*rgb[3*a+1]+v*rgb[3*b+1]+ww*rgb[3*d+1]+.5f);
                int bb=(int)(u*rgb[3*a+2]+v*rgb[3*b+2]+ww*rgb[3*d+2]+.5f);
                rr=rr<0?0:rr>255?255:rr;gg=gg<0?0:gg>255?255:gg;bb=bb<0?0:bb>255?255:bb;
                out[ix]=(uint16_t)(((rr>>3)<<11)|((gg>>2)<<5)|(bb>>3));
            }
        }
    }
    free(q);free(z);return 0;
}
