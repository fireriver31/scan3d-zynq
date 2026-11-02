/* Exact integer replay of pl_mask._regions; no colour, geometry or thresholds.
 * Keep every intermediate area/strength value, original run order and roots.
 */
#include <stdint.h>
#include <stdlib.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif
static int32_t root(int32_t *parent,int32_t i) {
    while(parent[i]!=i){parent[i]=parent[parent[i]];i=parent[i];}
    return i;
}
API int32_t scan_regions4(const uint8_t *mask,const uint8_t *strong,int32_t h,int32_t w,
    int32_t capacity,int32_t *runs,int32_t *labels,int32_t *area,int32_t *strength) {
    if(h<0||w<0||h>480||w>800||capacity<0)return -1;
    int32_t rowcap=(w+1)/2;
    int32_t *previous=(int32_t*)malloc((size_t)(rowcap+1)*3*sizeof(int32_t));
    int32_t *current=(int32_t*)malloc((size_t)(rowcap+1)*3*sizeof(int32_t));
    if(!previous||!current){free(previous);free(current);return -2;}
    int32_t nr=0,np=0;
    for(int32_t y=0;y<h;y++) {
        int32_t nc=0,cursor=0,x=0;
        while(x<w) {
            if(!mask[(size_t)y*w+x]){x++;continue;}
            int32_t start=x,seed=0;
            while(x<w && mask[(size_t)y*w+x]){seed+=(strong[(size_t)y*w+x]!=0);x++;}
            int32_t stop=x,node=nr;
            if(nr>=capacity){free(previous);free(current);return -3;}
            labels[node]=node;area[node]=stop-start;strength[node]=seed;
            while(cursor<np && previous[3*cursor+1]<=start)cursor++;
            int32_t j=cursor;
            while(j<np && previous[3*j]<stop) {
                int32_t r=root(labels,previous[3*j+2]),n=root(labels,node);
                if(r!=n){labels[r]=n;area[n]+=area[r];strength[n]+=strength[r];}
                j++;
            }
            current[3*nc]=start;current[3*nc+1]=stop;current[3*nc+2]=node;nc++;
            runs[4*nr]=y;runs[4*nr+1]=start;runs[4*nr+2]=stop;runs[4*nr+3]=node;nr++;
        }
        int32_t *swap=previous;previous=current;current=swap;np=nc;
    }
    for(int32_t i=0;i<nr;i++)labels[i]=root(labels,i);
    free(previous);free(current);return nr;
}
