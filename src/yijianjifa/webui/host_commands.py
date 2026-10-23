"""Shared native commands for the packaged Mac and Windows interface."""
import json
from pathlib import Path


def make_host(*,api,engine,window,webview,exit_started,finished_files,finished_deletion,
              deletion_recovery_pending,cover_response,drops,drop_access,media,excel,output,
              voice_catalog,volc_config,voices,play_sound,copy_text,request_exit,cloud_voice=None):
    class Host:
        def dispatch(self,raw,*,trusted_origin=False):
            if exit_started.is_set():
                return {'v':1,'request_id':None,'ok':False,'error':{'code':'app_closing','message':'程序正在退出，请等待当前操作结束。','retryable':False}}
            if trusted_origin:
                try:
                    from yijianjifa.app_services.library_queries import _unique_object
                    if type(raw) is not str or len(raw.encode('utf-8'))>16384:return api._error('invalid_request',None)
                    request=json.loads(raw,object_pairs_hook=_unique_object)
                    if type(request) is not dict or set(request)!={'v','request_id','operation','params'}:return api._error('invalid_request',None)
                    import re
                    rid=request['request_id'];op=request['operation'];p=request['params']
                    if type(request['v']) is not int or request['v']!=1 or type(rid) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',rid) or type(op) is not str or type(p) is not dict:return api._error('invalid_request',None)
                    if op in {'image_posts.import','image_posts.drop','image_posts.add_images','image_posts.add_images.drop',
                              'image_posts.metadata.import','image_posts.template.export','image_posts.export','image_posts.preview'}:
                        def image_picker(operation, params):
                            if operation=='image_posts.import' and params['batch'] or operation=='image_posts.export':
                                return window.create_file_dialog(webview.FileDialog.FOLDER,allow_multiple=False)
                            if operation=='image_posts.template.export':
                                return window.create_file_dialog(webview.FileDialog.SAVE,save_filename='图文文案导入模板.xlsx',file_types=('Excel (*.xlsx)',))
                            if operation=='image_posts.metadata.import':
                                return window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Excel (*.xlsx)',))
                            return window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=True,
                                file_types=('Image (*.png;*.jpg;*.jpeg;*.webp;*.bmp)',))
                        return api._image_posts.native(request,image_picker,drops,media)
                    if op in {'download.output.choose','download.models.import','download.subtitle.export','download.local.choose','download.local.drop'}:
                        def picker(operation,params):
                            if operation=='download.local.drop':
                                try:return drops.consume(params['token'])
                                except ValueError:
                                    from yijianjifa.features.downloads.contracts import DownloadError
                                    raise DownloadError('local_reselect_required') from None
                            if operation=='download.local.choose':
                                return window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=True,file_types=('Video (*.mp4;*.mov;*.m4v)',))
                            if operation=='download.output.choose':
                                return window.create_file_dialog(webview.FileDialog.FOLDER,allow_multiple=False)
                            if operation=='download.models.import':
                                return window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Recognition model (*.bin;*.onnx)',))
                            return window.create_file_dialog(webview.FileDialog.SAVE,save_filename='字幕.'+params['format'],file_types=('Subtitles (*.'+params['format']+')',))
                        return api.dispatch_download_native(raw,picker,trusted_origin=True)
                    if op in {'finished.delete.preview','finished.delete.apply'}:
                        try:
                            if op=='finished.delete.preview':
                                if set(p)!={'ids'}:return api._error('invalid_request',rid)
                                finished_files.validate_export(p['ids'],False)
                                data=finished_deletion.preview(p['ids'])
                                data['recovery_pending']=deletion_recovery_pending
                            else:
                                if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:return api._error('invalid_request',rid)
                                data=finished_deletion.delete(p['id'])
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except RuntimeError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'delete_blocked','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'delete_failed','message':'删除未完成，已保留恢复记录。请检查文件权限后重试。','retryable':True}}
                    if op in {'finished.cover.read','finished.cover.frame','finished.cover.choose'}:
                        try:
                            expected={'id','kind','frame_ms'} if op=='finished.cover.frame' else {'id'}
                            if set(p)!=expected or type(p['id']) is not str or not 1<=len(p['id'])<=64:return api._error('invalid_request',rid)
                            if op=='finished.cover.read':
                                data,path=api._covers.read(p['id']);data=cover_response(data,path)
                            else:
                                if op=='finished.cover.frame':token=api._covers.frame(p['id'],p['kind'],p['frame_ms'])
                                else:
                                    selected=window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Image (*.png;*.jpg;*.jpeg;*.webp)',))
                                    token=api._covers.image(p['id'],selected or [])
                                if token:
                                    data,path=api._covers.preview(token);data=cover_response(data,path,token)
                                else:data={'cancelled':True}
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'cover_invalid','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'cover_failed','message':'封面操作未完成，请检查文件和权限。','retryable':False}}
                    if op in {'finished.import','finished.drop','finished.export'}:
                        try:
                            if op=='finished.import':
                                if set(p)!={'folder'} or type(p['folder']) is not bool:return api._error('invalid_request',rid)
                                selected=window.create_file_dialog(webview.FileDialog.FOLDER if p['folder'] else webview.FileDialog.OPEN,
                                    allow_multiple=not p['folder'],file_types=() if p['folder'] else ('Video (*.mp4;*.mov;*.mkv;*.avi;*.m4v;*.webm)',))
                                data=finished_files.import_files(selected or [])
                            elif op=='finished.drop':
                                if set(p)!={'token'} or type(p['token']) is not str or not 1<=len(p['token'])<=128:return api._error('invalid_request',rid)
                                data=finished_files.import_files(drops.consume(p['token']))
                            else:
                                if set(p)!={'ids','include_metadata'}:return api._error('invalid_request',rid)
                                finished_files.validate_export(p['ids'],p['include_metadata'])
                                selected=window.create_file_dialog(webview.FileDialog.FOLDER,allow_multiple=False)
                                data=finished_files.export(p['ids'],selected or [],include_metadata=p['include_metadata'])
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError:
                            return api._error('invalid_request',rid)
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'finished_files_failed','message':'成品文件操作未完成，请检查选择和权限。','retryable':False}}
                    if op in {'task.diagnostic.export','task.problem.copy','task.execution.copy'}:
                        if set(p)!={'id'} or type(p['id']) is not str or not 1<=len(p['id'])<=64:return api._error('invalid_request',rid)
                        try:
                            from yijianjifa.app_services.task_center_access import TaskCenterAccess
                            from yijianjifa.features.tasks.diagnostics import DiagnosticBundle
                            if op=='task.execution.copy':
                                copy_text(TaskCenterAccess(engine).execution_log_text(p['id']))
                                return {'v':1,'request_id':rid,'ok':True,'data':{'copied':True}}
                            data=TaskCenterAccess(engine).diagnostic(p['id'])
                            if op=='task.diagnostic.export':
                                selected=window.create_file_dialog(webview.FileDialog.SAVE,save_filename='一剪即发_任务诊断.json',file_types=('JSON (*.json)',))
                                if selected:
                                    target=Path(selected[0] if isinstance(selected,(list,tuple)) else selected)
                                    if target.suffix.lower()!='.json':raise ValueError()
                                    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
                                result={'saved':bool(selected)}
                            else:
                                content='\n\n'.join(DiagnosticBundle(i).copyable_problem_text() for i in data['issues']) or '任务 '+p['id']+' 当前没有问题记录。'
                                copy_text(content)
                                result={'copied':True}
                            return {'v':1,'request_id':rid,'ok':True,'data':result}
                        except Exception:return api._error('internal_error',rid)
                    if op=='settings.sound.preview':
                        if p:return api._error('invalid_request',rid)
                        enabled=api._mix.settings.notification_sound_enabled
                        if enabled:play_sound()
                        return {'v':1,'request_id':rid,'ok':True,'data':{'requested':enabled}}
                    if op in {'settings.output.read','settings.output.choose'}:
                        try:
                            if p:raise ValueError('保存位置请求无效。')
                            if op=='settings.output.read':data=output.read()
                            else:
                                selected=window.create_file_dialog(webview.FileDialog.FOLDER,allow_multiple=False)
                                data=output.choose(selected)
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'output_invalid','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'output_failed','message':'无法更改成品保存位置，请稍后重试。','retryable':True}}
                    if op.startswith('voice.cloud.') and cloud_voice is not None:
                        try:
                            from .cloud_voice_commands import dispatch_cloud_voice
                            data=dispatch_cloud_voice(cloud_voice,api,op,p,window,webview.FileDialog)
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'cloud_voice_invalid','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'cloud_voice_unavailable','message':'云配音操作未完成，请检查网络或账号后重试。原额度和任务保留。','retryable':True}}
                    if op in {'voice.catalog','voice.selection.save','voice.standard'}:
                        try:
                            if op=='voice.catalog':
                                if p:raise ValueError('请求无效。')
                                data=voice_catalog.read()
                            elif op=='voice.standard':
                                if set(p)!={'voice'}:raise ValueError('请求无效。')
                                data=voice_catalog.standard(p['voice'])
                            else:
                                if set(p)!={'selected','favorites'}:raise ValueError('请求无效。')
                                data=voice_catalog.save(p['selected'],p['favorites'])
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'voice_invalid','message':str(exc),'retryable':False}}
                        except Exception:return api._error('internal_error',rid)
                    if op in {'voice.settings.read','voice.settings.save'}:
                        return {'v':1,'request_id':rid,'ok':False,'error':{
                            'code':'voice_settings_retired',
                            'message':'客户端不再提供个人配音服务配置，请选择系统配音或云配音。',
                            'retryable':False}}
                    if op in {'voice.list','voice.audition','voice.audition.text'}:
                        try:
                            if op=='voice.list':
                                if p:raise ValueError('音色请求无效')
                                data=voices.voices()
                            elif op=='voice.audition.text':
                                if set(p)!={'text','voice','speed'}:raise ValueError('请求无效。')
                                data=voices.audition_text(p['text'],p['voice'],p['speed'])
                            else:
                                if set(p)!={'copy_id','voice','speed'}:raise ValueError('试听请求无效')
                                data=voices.audition(p['copy_id'],p['voice'],p['speed'])
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'voice_invalid','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'voice_failed','message':'配音失败或超时，请检查所选音色和配音设置后重试。','retryable':True}}
                    if op in {'excel.preview','excel.drop','excel.apply','excel.template','excel.export','excel.open_saved'}:
                        try:
                            if op=='excel.open_saved':
                                if set(p)!={'token','action'}:raise ValueError('请求无效。')
                                data=excel.open_saved(p['token'],p['action'])
                            elif op=='excel.drop':
                                if set(p)!={'token'} or type(p['token']) is not str or len(p['token'])!=32:raise ValueError('拖入请求无效。')
                                data=drop_access.excel(p['token'])
                            elif op=='excel.preview':
                                if p:raise ValueError('请求无效。')
                                selected=window.create_file_dialog(webview.FileDialog.OPEN,allow_multiple=False,file_types=('Excel (*.xlsx)',))
                                data=excel.preview(selected[0]) if selected else {'cancelled':True}
                            elif op=='excel.apply':
                                if set(p)!={'token','approved_ids','category_ids'}:raise ValueError('请求无效。')
                                data=excel.apply(p['token'],p['approved_ids'],p['category_ids'])
                            else:
                                from yijianjifa.features.library.excel_import import export_template,export_with_ids
                                from yijianjifa.features.library.copy_service import CopyRepository
                                if op=='excel.template' and p:raise ValueError('请求无效。')
                                if op=='excel.export' and (set(p)!={'ids'} or type(p['ids']) is not list or not all(type(x) is str and 1<=len(x)<=64 for x in p['ids'])):raise ValueError('请求无效。')
                                selected=window.create_file_dialog(webview.FileDialog.SAVE,save_filename='一剪即发_文案导入模板.xlsx' if op=='excel.template' else '一剪即发_文案.xlsx',file_types=('Excel (*.xlsx)',))
                                if not selected:data={'cancelled':True}
                                else:
                                    target=Path(selected[0] if isinstance(selected,(list,tuple)) else selected)
                                    if target.suffix.lower()!='.xlsx':raise ValueError('请保存为.xlsx文件。')
                                    if op=='excel.template':export_template(target)
                                    else:export_with_ids(CopyRepository(engine),p['ids'],target)
                                    data=excel.register_saved(target)
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except ValueError as exc:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'excel_invalid','message':str(exc),'retryable':False}}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'excel_failed','message':'表格操作失败，请检查文件、模板格式和权限。','retryable':False}}
                    if op in {'media.play','media.position','media.expand','media.stop'}:
                        try:
                            if set(p)!=({'token'} if op in {'media.stop','media.expand'} else {'token','surface'}) or type(p['token']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{32}',p['token']) or not media.native_video:raise ValueError()
                            if op=='media.play':data=media.play_native(p['token'],window,surface=p['surface'])
                            elif op=='media.position':data=media.position_native(p['token'],window,p['surface'])
                            elif op=='media.expand':data=media.expand_native(p['token'],window)
                            else:window.stop_media(p['token']);data={'stopped':True}
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'media_unavailable','message':'视频预览未能打开，请重新选择视频后重试。','retryable':True}}
                    if op in {'media.import','media.drop','media.preview','media.thumbnail','media.reveal','media.preference'}:
                        try:
                            if op=='media.drop':
                                if set(p)!={'kind','token','keep_copy','category_ids'} or p['kind'] not in ('video','music') or type(p['token']) is not str or len(p['token'])!=32 or type(p['keep_copy']) is not bool or type(p['category_ids']) is not list or len(p['category_ids'])>100 or not all(type(x) is str and 1<=len(x)<=64 for x in p['category_ids']):raise ValueError()
                                data=drop_access.media(p['token'],kind=p['kind'],keep_copy=p['keep_copy'],category_ids=p['category_ids'])
                            elif op=='media.import':
                                if set(p)!={'kind','folder','keep_copy','category_ids'} or p['kind'] not in ('video','music') or type(p['folder']) is not bool or type(p['keep_copy']) is not bool or type(p['category_ids']) is not list or len(p['category_ids'])>100 or not all(type(x) is str and 1<=len(x)<=64 for x in p['category_ids']):raise ValueError()
                                filters=('视频 (*.mp4;*.mov;*.mkv;*.avi;*.m4v;*.webm)',) if p['kind']=='video' else ('音乐 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg)',)
                                selected=window.create_file_dialog(webview.FileDialog.FOLDER if p['folder'] else webview.FileDialog.OPEN,allow_multiple=not p['folder'],file_types=() if p['folder'] else filters)
                                data=media.import_files(p['kind'],selected or [],keep_copy=p['keep_copy'],category_ids=p['category_ids'],folder=p['folder'])
                            elif op=='media.preference':
                                if set(p)!={'kind'} or p['kind'] not in ('video','music'):raise ValueError()
                                from yijianjifa.features.library.preferences import LibraryPreferences
                                data={'keep_copy':LibraryPreferences(engine).keep_copy(p['kind'])}
                            else:
                                expected={'kind','id','original'} if op=='media.reveal' else {'kind','id'}
                                if set(p)!=expected or p['kind'] not in ('video','music','finished') or type(p['id']) is not str or not 1<=len(p['id'])<=64:raise ValueError()
                                if op=='media.reveal':
                                    if type(p['original']) is not bool:raise ValueError()
                                    data=media.reveal(p['kind'],p['id'],original=p['original'])
                                elif op=='media.thumbnail':data=media.thumbnail(p['kind'],p['id'])
                                else:data=media.preview(p['kind'],p['id'])
                            return {'v':1,'request_id':rid,'ok':True,'data':data}
                        except Exception:
                            return {'v':1,'request_id':rid,'ok':False,'error':{'code':'media_unavailable','message':'无法操作这份媒体，请检查分类、文件位置、权限或格式。','retryable':False}}
                    if set(request)=={'v','request_id','operation','params'} and request['v']==1 and request['operation']=='app.exit' and request['params']=={}:
                        if not exit_started.is_set():
                            request_exit()
                        return {'v':1,'request_id':request['request_id'],'ok':True,'data':{'exiting':True}}
                except (ValueError,TypeError):pass
            result=api.dispatch(raw,trusted_origin=trusted_origin)
            if result.get('ok') and trusted_origin:
                op=json.loads(raw)['operation']
                if op in {'mix.submit','mix.daily.submit','task.resume'}:api.start_queue()
            return result

    return Host()
