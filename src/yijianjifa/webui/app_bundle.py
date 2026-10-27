"""Package-only view templates and private-world scripts; no remote page loads."""
import json
import re
from yijianjifa import APP_VERSION
from pathlib import Path


def application_script():
    assets=Path(__file__).parent/'assets'
    templates={}
    for name in ('library','mixing','finished','presets','tasks','accounts','publish','multi_publish','commerce','voices','downloads'):
        html=(assets/(('publish' if name=='multi_publish' else name)+'.html')).read_text(encoding='utf-8')
        templates[name]={'body':re.search(r'<body[^>]*>([\s\S]*?)</body>',html,re.I).group(1),
                         'css':'\n'.join(re.findall(r'<style[^>]*>([\s\S]*?)</style>',html,re.I))+'\n'+(assets/'common.css').read_text(encoding='utf-8')+'\n'+(assets/'image_content.css').read_text(encoding='utf-8')}
    return ('const P011Version='+json.dumps(APP_VERSION)+';\n'+'const P011Templates='+json.dumps(templates,ensure_ascii=False)+';\n'+
            '\n'.join((assets/name).read_text(encoding='utf-8') for name in ('features.js','write_intent.js','selection_toolbar.js','notifications.js','use_term.js','publish_intervention.js','publish_progress.js','date_picker.js','router.js','startup_recovery.js','category_manager.js','library.js','mixing_form.js','account_picker.js','mixing_publish.js','mixing_advanced.js','mixing_execution.js','mixing.js','image_posts.js','finished.js','presets.js','cloud_voice.js','voices.js','download_subtitles.js','downloads.js','multi_publish_platform_fields.js','multi_publish_targets.js','tasks.js','account_login.js','account_publish_settings.js','accounts.js','publish_form.js','cover_batch.js','multi_publish_client.js','publish_daily_schedule.js','publish_execution.js','multi_publish_ui.js','image_publish.js','publish.js','multi_publish.js','commerce_form.js','commerce.js'))+
            '\nP011Router.go("mixing");void P011StartupRecovery();')
