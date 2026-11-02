"""Offline .bit/.hwh metadata audit. No PYNQ import, MMIO or download."""
import argparse,hashlib,json,re,struct,sys
from pathlib import Path
import xml.etree.ElementTree as ET

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def read_bit_header(path):
    with path.open('rb') as f:
        def read(n):
            data=f.read(n)
            if len(data)!=n:raise ValueError('truncated bitstream header')
            return data
        n=struct.unpack('>H',read(2))[0]
        if not 1<=n<=256:raise ValueError('unsupported .bit header format')
        read(n)
        n=struct.unpack('>H',read(2))[0]
        if n!=1:raise ValueError('unsupported .bit header tag encoding')
        tag=read(1);fields={}
        for _ in range(8):
            if tag==b'e':
                size=struct.unpack('>I',read(4))[0]
                if size>path.stat().st_size-f.tell():raise ValueError('truncated configuration payload')
                fields['payload_bytes']=size;return fields
            if tag not in (b'a',b'b',b'c',b'd'):raise ValueError('unrecognized .bit metadata tag')
            size=struct.unpack('>H',read(2))[0]
            fields[{b'a':'design',b'b':'part',b'c':'date',b'd':'time'}[tag]]=read(size).rstrip(b'\0').decode('ascii')
            tag=read(1)
        raise ValueError('missing .bit payload tag')

def audit(bit,hwh,expected_ips=()):
    bit,hwh=Path(bit),Path(hwh)
    result=dict(schema_version=1,scope='offline_metadata_only',passed=False,
        proves_loaded_hardware=False,clock_readback_performed=False,errors=[])
    for path in (bit,hwh):
        if not path.is_file():result['errors'].append('missing file: '+path.name)
    if result['errors']:return result
    if bit.stem!=hwh.stem:result['errors'].append('bit/hwh basenames differ')
    try:
        header=read_bit_header(bit);root=ET.parse(str(hwh)).getroot()
        elements=list(root.iter())
        tag=lambda e:e.tag.rsplit('}',1)[-1].upper()
        info=next((dict(e.attrib) for e in elements if tag(e)=='SYSTEMINFO'),{})
        modules=sorted(set(e.attrib['INSTANCE'] for e in elements if tag(e)=='MODULE' and 'INSTANCE'in e.attrib))
        ranges=[];seen=set()
        for e in elements:
            if tag(e)!='MEMRANGE':continue
            item={k:e.attrib.get(k) for k in ('INSTANCE','BASEVALUE','HIGHVALUE','MEMTYPE','SLAVEBUSINTERFACE')}
            key=tuple(item.values())
            if key not in seen:seen.add(key);ranges.append(item)
        for name in expected_ips:
            if name not in modules:result['errors'].append('missing expected IP: '+name)
        normalize=lambda s:re.sub(r'^xc','',s.lower()).split('-')[0]
        device=info.get('DEVICE','');package=info.get('PACKAGE','')
        if not device or not package or not header.get('part'):
            result['errors'].append('device/package metadata unavailable; part pairing not proven')
        elif normalize(header['part'])!=normalize(device+package):result['errors'].append('bit/hwh device or package mismatch')
        clocks=[]
        for e in elements:
            if tag(e)=='PARAMETER' and re.fullmatch(r'PCW_FPGA[0-3]_PERIPHERAL_FREQMHZ',e.attrib.get('NAME','')):
                clocks.append(dict(name=e.attrib['NAME'],declared_mhz=e.attrib.get('VALUE')))
        result.update(bit_header=header,hwh_system=info,hwh_vivado_version=root.attrib.get('VIVADOVERSION'),
            modules=modules,memory_ranges=ranges,declared_clocks=clocks,
            sha256=dict(bit=digest(bit),hwh=digest(hwh)))
    except (ValueError,UnicodeError,ET.ParseError,struct.error,OSError) as exc:result['errors'].append(str(exc))
    result['passed']=not result['errors']
    return result

def main():
    p=argparse.ArgumentParser(description='只读核验bit/hwh，不加载FPGA、不读写寄存器')
    p.add_argument('--bit',required=True);p.add_argument('--hwh',required=True)
    p.add_argument('--expect-ip',action='append',default=[]);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.resolve() in (Path(a.bit).resolve(),Path(a.hwh).resolve()):p.error('output must not overwrite an input')
    result=audit(a.bit,a.hwh,a.expect_ip);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('PASS' if result['passed'] else 'FAIL',out)
    return 0 if result['passed'] else 2
if __name__=='__main__':sys.exit(main())
