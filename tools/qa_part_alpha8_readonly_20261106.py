"""One hidden in-memory actual-bank Alpha8 feasibility experiment; no edits."""
from copy import copy
from dataclasses import replace
import hashlib
import itertools
import json
import math
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SOURCES=('desktop_pet/part_interaction.py','desktop_pet/eye_tracking.py')


def sources():
    paths=[ROOT/p for p in SOURCES]
    paths+=list((ROOT/'assets/character/interactions').rglob('*.json'))
    paths+=list((ROOT/'assets/character/interactions').rglob('*.png'))
    paths+=list((ROOT/'assets/expressions').glob('*.png'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def run():
    from PySide6.QtCore import QCoreApplication,QPoint,QPointF,Qt
    from PySide6.QtGui import QColor,QImage,QPainter
    from desktop_pet.part_interaction import PartInteraction,_alpha_at
    app=QCoreApplication.instance() or QCoreApplication([])
    bank=PartInteraction()
    assert len(bank.rigs)==7
    originals={id(value):value for value in bank.rigs.values()}
    inventory=[];failures=[];proofs={'alpha_pixels':0,'actual_alpha_at_samples':0,
        'source_over_alpha_pixels':0,'destination_pixels':0,'body_frames':0,
        'contains_rest_pixels':0,'contains_rotated_samples':0,'interaction_hit_samples':0}
    def alpha(image):
        raw=bytes(image.constBits());w,h=image.width(),image.height();stride=image.bytesPerLine()
        if image.format()==QImage.Format.Format_Alpha8:
            return b''.join(raw[y*stride:y*stride+w] for y in range(h))
        assert image.format()==QImage.Format.Format_ARGB32_Premultiplied
        offset=3 if sys.byteorder=='little' else 0
        return b''.join(raw[y*stride+offset:y*stride+w*4:4] for y in range(h))
    def rgba(image):return bytes(image.constBits())
    def painter_result(image,mode):
        target=QImage(image.size(),QImage.Format.Format_ARGB32_Premultiplied)
        target.fill(Qt.GlobalColor.transparent if mode==QPainter.CompositionMode.CompositionMode_SourceOver else QColor(31,79,143,173))
        painter=QPainter(target);assert painter.isActive();painter.setCompositionMode(mode)
        painter.drawImage(QPoint(),image);painter.end();return target
    modes=(QPainter.CompositionMode.CompositionMode_SourceOver,
        QPainter.CompositionMode.CompositionMode_DestinationIn,QPainter.CompositionMode.CompositionMode_DestinationOut)
    def converted(label,image):
        clone=image.convertToFormat(QImage.Format.Format_Alpha8)
        a,b=alpha(image),alpha(clone)
        assert a==b,label
        proofs['alpha_pixels']+=len(a)
        inventory.append({'label':label,'width':image.width(),'height':image.height(),
            'original_bytes':image.sizeInBytes(),'alpha8_bytes':clone.sizeInBytes(),
            'original_stride':image.bytesPerLine(),'alpha8_stride':clone.bytesPerLine(),
            'alpha_values':len(set(a)),'original_cache_key':image.cacheKey(),'converted_cache_key':clone.cacheKey()})
        seen={}
        for offset,value in enumerate(a):
            if value not in seen:seen[value]=offset
            if len(seen)==256:break
        for value,offset in seen.items():
            point=QPointF(offset%image.width()+.5,offset//image.width()+.5)
            assert _alpha_at(image,point)==_alpha_at(clone,point)==value,label
            proofs['actual_alpha_at_samples']+=1
        for point in (QPointF(-1,-1),QPointF(image.width(),image.height()),QPointF(math.nan,0)):
            assert _alpha_at(image,point)==_alpha_at(clone,point)==0
        for mode in modes:
            left,right=painter_result(image,mode),painter_result(clone,mode)
            equal=alpha(left)==alpha(right) if mode==modes[0] else rgba(left)==rgba(right)
            if not equal:failures.append({'label':label,'mode':mode.name})
            proofs['source_over_alpha_pixels' if mode==modes[0] else 'destination_pixels']+=image.width()*image.height()
        return clone
    cloned={}
    for identity,rig in originals.items():
        stem=next(key for key,value in bank.rigs.items() if value is rig)
        parts=tuple(replace(part,hit_mask=converted(f'{stem}:hit:{part.name}',part.hit_mask))
            if part.hit_mask is not None else replace(part) for part in rig.parts)
        repairs={name:(point,converted(f'{stem}:repair:{name}',mask)) for name,(point,mask) in rig.repair_masks.items()}
        cloned[identity]=replace(rig,cutout_mask=converted(f'{stem}:cutout',rig.cutout_mask),parts=parts,repair_masks=repairs)
    candidate=copy(bank)
    candidate.rigs={name:cloned[id(rig)] for name,rig in bank.rigs.items()}
    candidate._rigs_by_pixels={key:(name,cloned[id(rig)]) for key,(name,rig) in bank._rigs_by_pixels.items()}
    candidate._body_cache={};candidate._body_bounds_cache={};candidate._elapsed={}
    def resident(rigs):
        images={}
        for rig in {id(rig):rig for rig in rigs.values()}.values():
            items=[rig.source,rig.base,rig.cutout_mask,*[part.image for part in rig.parts],
                *[part.hit_mask for part in rig.parts if part.hit_mask is not None],*[mask for _,mask in rig.repair_masks.values()]]
            for image in items:images.setdefault(image.cacheKey(),image.sizeInBytes())
        return sum(images.values())
    before_bytes,after_bytes=resident(bank.rigs),resident(candidate.rigs)
    for stem,rig in bank.rigs.items():
        clone=candidate.rigs[stem]
        bank.bind_source(rig.source);candidate.bind_source(rig.source)
        names=tuple(sorted(part.name for part in rig.parts))
        for length in range(1,len(names)+1):
            for subset in itertools.combinations(names,length):
                left,right=bank._body_for_parts(None,subset),candidate._body_for_parts(None,subset)
                if rgba(left)!=rgba(right):failures.append({'rig':stem,'body_subset':subset})
                proofs['body_frames']+=1
        for part,other in zip(rig.parts,clone.parts):
            if part.hit_mask is None:continue
            for y in range(part.hit_mask.height()):
                for x in range(part.hit_mask.width()):
                    point=part.hit_origin+QPointF(x+.5,y+.5)
                    if part.contains(point,0,use_hit_mask=True)!=other.contains(point,0,use_hit_mask=True):
                        failures.append({'rig':stem,'contains':part.name,'x':x,'y':y})
                    proofs['contains_rest_pixels']+=1
            for angle in (-part.amplitude_degrees,part.amplitude_degrees):
                transform=part.transform(angle)
                for y in range(0,part.hit_mask.height(),max(1,part.hit_mask.height()//8)):
                    for x in range(0,part.hit_mask.width(),max(1,part.hit_mask.width()//8)):
                        point=transform.map(part.hit_origin+QPointF(x+.5,y+.5))
                        assert part.contains(point,angle,use_hit_mask=True)==other.contains(point,angle,use_hit_mask=True)
                        proofs['contains_rotated_samples']+=1
        for name in names:bank.trigger(name);candidate.trigger(name)
        bank.advance(.065);candidate.advance(.065)
        for y in range(0,1024,19):
            for x in range(0,1024,19):
                point=QPointF(x+.5,y+.5)
                assert bank.contains(point)==candidate.contains(point)
                assert bank.hit_test(point)==candidate.hit_test(point)
                proofs['interaction_hit_samples']+=1
        print(json.dumps({'completed_rig':stem,'failures':len(failures)},ensure_ascii=True),flush=True)
    return {'passed':not failures,'failures':failures,'rigs':list(bank.rigs),'unique_rigs':len(originals),
        'inventory':inventory,'proofs':proofs,'resident_bank_bytes':{'before':before_bytes,'after':after_bytes,
            'saved':before_bytes-after_bytes,'saved_MiB':(before_bytes-after_bytes)/1024**2},
        'mask_bytes':{'before':sum(row['original_bytes'] for row in inventory),'after':sum(row['alpha8_bytes'] for row in inventory)},
        'scope':'One hidden actual production PartInteraction bank and in-memory Alpha8 clone; stored cutout/repair/hit masks only. Canvas/base/part colors, scratch-mask target format and source/assets are unchanged. SourceOver compares alpha only; DestinationIn/Out and actual body composites compare all RGBA bytes. Rest contains exhausts cropped hit-mask pixel centers; rotated/full-interaction samples are bounded grids. Canonical body frames use eye_rig=None. Counts are QImage resident buffer bytes, not RSS/power or release-process proof.'}


def main():
    output=ROOT/'artifacts/task-audit/part-alpha8-readonly-20261106';output.mkdir(parents=True,exist_ok=False)
    before=sources();blocked=[];started=time.perf_counter()
    def deny(*a,**k):blocked.append('network');raise AssertionError('NETWORK_FORBIDDEN')
    try:
        with patch.object(socket,'getaddrinfo',deny),patch.object(socket.socket,'connect',deny),patch.object(socket.socket,'connect_ex',deny):result=run()
    except Exception as error:
        import traceback
        (output/'exception.log').write_text(traceback.format_exc(),encoding='utf8');result={'passed':False,'error_type':type(error).__name__}
    after=sources();result.update(source_before=before,source_after=after,source_unchanged=before==after,
        elapsed_seconds=time.perf_counter()-started,blocked_network_attempts=blocked,real_model_requests=0,
        visible_window_created=False,user_profile_used=False,system_clipboard_used=False)
    result['passed']=result['passed'] and before==after and not blocked
    (output/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({key:value for key,value in result.items() if key not in ('source_before','source_after','inventory')},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
