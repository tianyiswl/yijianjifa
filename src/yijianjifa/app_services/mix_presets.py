"""Translate complete local mix controls to the existing typed preset store."""
from yijianjifa.features.presets.models import MixPreset, AudioSettings, SubtitleSettings
from yijianjifa.features.presets.service import PresetService
from yijianjifa.features.library.copy_service import CategoryRepository
from yijianjifa.storage.db import session_scope
from yijianjifa.storage.models import MediaAsset


def to_model(name, p):
    p={**p};p.setdefault('spoken_style',{'size':75,'y_percent':77});p.setdefault('full_style',{'size':100,'y_percent':12})
    material=p.get('material_categories',[]);copies=p.get('copy_categories',[])
    music=p.get('background_music',{'mode':'none'});transition=p.get('transition',{'mode':'none'})
    return MixPreset(name=name.strip(),use_copy=p.get('use_copy',True),material_scope='categories' if material else 'all',material_category_ids=material,
        copy_scope='categories' if copies else 'all',copy_category_ids=copies,
        quantity=p['quantity'],aspect_ratio=p.get('aspect_ratio','9:16'),resolution=p.get('resolution','720p'),
        frame_fit=p.get('frame_fit','preserve_blur'),background_music_mode=music['mode'],
        background_music_id=music.get('music_id'),music_category_ids=music.get('category_ids',[]),
        transition_mode=transition['mode'],transition_name=transition.get('name'),transition_duration_ms=transition.get('duration_ms',500),
        audio=AudioSettings(keep_original_sound=p.get('keep_original_audio',True),tts_voice=p.get('voice') or 'system_default',
            tts_speed=p.get('speed',1),original_volume=p.get('original_gain',0),voice_volume=p.get('voice_gain',1),music_volume=p.get('music_gain',.25)),
        subtitles=SubtitleSettings(spoken_enabled=p.get('spoken_enabled',True),full_time_enabled=p.get('full_enabled',False)),
        **{k:p[k] for k in ('total_ms','segment_ms','total_range_ms','segment_range_ms','copy_mode','sentence_cut','spoken_style','full_style') if k in p})


def to_params(m):
    # Legacy single-style presets keep their old values where supported; no silent widening of scopes.
    legacy={'font':'sans' if m.subtitles.font=='system_default' else m.subtitles.font,
        'size':m.subtitles.font_size,'chars':m.subtitles.max_chars_per_line,
        'position':'middle' if m.subtitles.position=='center' else m.subtitles.position,'color':'#ffffff'}
    return dict(use_copy=m.use_copy,quantity=m.quantity,material_categories=list(m.material_category_ids),copy_categories=list(m.copy_category_ids),
        total_ms=m.total_ms,segment_ms=m.segment_ms,total_range_ms=m.total_range_ms,segment_range_ms=m.segment_range_ms,
        copy_mode=m.copy_mode,sentence_cut=m.sentence_cut,resolution=m.resolution,aspect_ratio=m.aspect_ratio,frame_fit=m.frame_fit,
        voice=None if m.audio.tts_voice=='system_default' else m.audio.tts_voice,speed=m.audio.tts_speed,
        keep_original_audio=m.audio.keep_original_sound,original_gain=m.audio.original_volume,voice_gain=m.audio.voice_volume,music_gain=m.audio.music_volume,
        spoken_enabled=m.subtitles.spoken_enabled,full_enabled=m.subtitles.full_time_enabled,
        spoken_style=m.spoken_style or legacy,full_style=m.full_style or legacy,
        background_music=dict(mode=m.background_music_mode,music_id=m.background_music_id,category_ids=list(m.music_category_ids)),
        transition=dict(mode=m.transition_mode,name=m.transition_name,duration_ms=m.transition_duration_ms))


def read(engine, pid):
    service=PresetService(engine);stored=service.get(pid)
    if not isinstance(stored.preset,MixPreset):raise ValueError('not a mix preset')
    m=stored.preset;materialized=service.materialize(pid);missing=list(materialized.missing_category_ids)
    music_categories={c.id for c in CategoryRepository(engine).list('music')}
    missing.extend(i for i in m.music_category_ids if i not in music_categories)
    missing_music=False
    if m.background_music_mode=='fixed':
        with session_scope(engine) as session:
            item=session.get(MediaAsset,m.background_music_id) if m.background_music_id else None
            missing_music=item is None or item.kind!='music'
    params=to_params(m)
    # Old models allowed settings that the current renderer cannot honor. Require repair instead of guessing.
    from .desktop_api import DesktopApi
    invalid=False
    try:DesktopApi._validate('mix.submit',params)
    except (ValueError,TypeError):invalid=True
    return dict(id=pid,name=m.name,settings=params,state='needs_repair' if missing or missing_music or invalid else 'ready',
        missing_category_ids=list(dict.fromkeys(missing)),missing_music=missing_music,invalid_settings=invalid)
