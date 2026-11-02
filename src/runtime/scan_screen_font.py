"""MIT loader for Scan3D Raster CJK. scan_font.bin is licensed OFL-1.1."""
from pathlib import Path

VERSION = 103
CHARS = '!"#%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]_abcdefghijklmnopqrstuvwxyz{|}~²³…、。一三上下不与且丢两个中为主义之乎乏九也了争二于云亚些交亮人仅仍从他代以件份优会传估似但位低体何余作使例供依侧便保修候值偏做停储像允元充先光免入全八六共关其兼内再冒写冲决冻准减几凭凸凹出分切划列则初判别到制刷刻前剪割加动助匀包化匹区半单占卡印即却原去参叉及双反取受变叠口另只可台右号各合同后向否含启呈告周和响哑哪唯器噪回因围固图圆圈在地址坐块型域基填增声壳处备复外多够大太失头套好如始姿子孔字存它守完定实容宽密察寸对导封射将小少尚尝就尺尾局居屏属工左差已布带帧常平并序应底废度廓建开异弃式引张弱强归当录形彩影征径待得心必志态性总恢悄意感成或戳所手才打扩扫批找把抓投抛报拍拒拟择拷持指按挡损换据掉排接控推掩描提搜摄摆撕操支改放故效敏数整文斑斜断新方旋无既日旧时明映是显景暂暗曝更替最有朝期未本机杂束条来松板果架染查柱标校样核根格框桌档检模横次止正此步歧段每比毫气水求污没沿法泛波注洞洪流测消深混清源满漏演灯灰点照片版物特状独猜率现理生用由电画界留略疏疑白的益盖盘目直相省看真眼着瞬知短码破硬确碎碰示票禁离秒积称移稀程稳空立竖竞端笔符第等答简算管米精系素索红约纹线组细终经绑结绕给绝统继绪续维缓编缘缝缩缺网置考耗背能脑腿膜自至致色节若范获蓝融行衡补表被裁裂装要覆见观规视览觉角解触警计认记许论设访证评识诊译试详误请读调贝负败贴起超越足跑距跨跳身躺车转轮软轴载较辅输辨边达过运近返还这进远连迟述退送适选透逐通速遇遮避邻部都配采释里重量锁错镜长闭问间阈阴阶附际降限除陷隙集雕零需露青静非靠面顶顺须预颜饱首验骤高黄黑默齐（），：；薄产击勿十幕执收晰走钮'
_RAW = Path(__file__).with_name('scan_font.bin').read_bytes()
if len(_RAW) != len(CHARS) * 128:
    raise ValueError('scan_font.bin size does not match the character list')
_INDEX = {char: i for i, char in enumerate(CHARS)}


def packed(char):
    index = _INDEX.get(char)
    return None if index is None else _RAW[index * 128:(index + 1) * 128]
