"""Model-free source/archive gate. Does not import models or contact services."""
import argparse,ast,json,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REQUIRED={
 'frontend':['runtime/ui/app/index.html','runtime/ui/app/main.js','runtime/ui/app/server.py'],
 'llm':['runtime/ui/app/models_admin.py','runtime/llm/Switch-Girlfriend-LLM.ps1','runtime/llm/start_llama_task.bat','config/models.example.json'],
 'tts':['runtime/s2s_business/higgs_tts_handler.py','runtime/s2s_business/fish_tts_handler.py','runtime/higgs/higgs_worker.py','runtime/core/providers/adapters/qwen3_tts_adapter.py','third_party_modified/speech_to_speech_0.2.11/s2s_pipeline.py'],
 'voice':['runtime/ui/app/voice_library_admin.py','runtime/ui/app/ui/voice_library.js'],
 'emotion':['runtime/core/providers/delivery.py','runtime/core/providers/manifests/tts.json','runtime/ui/app/ui/girlfriend.js'],
 'avatar':['runtime/avatar/app/server/routes.py','runtime/ui/app/avatar-sync.js','runtime/ui/app/audio-router.js'],
 'reading':['runtime/reading/documents.py','runtime/ui/app/reading_admin.py','runtime/girlfriend_reading.py','third_party_modified/speech_to_speech_0.2.11/api/openai_realtime/reading_bridge.py'],
 'live':['runtime/live/service.py','runtime/live/router.py','runtime/live/barrage_bridge.py','runtime/ui/app/live_interaction.js','config/live.example.json'],
 'manager':['runtime/ui7860_manager/manager.py','runtime/ui7860_manager/Console.ps1','scripts/Manage-Project-Service.ps1','Start-AI-Girlfriend.cmd'],
}
def files(root):
    if (root/'.git').is_dir():
        names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=root,text=True).splitlines()
        return sorted({root/n for n in names if (root/n).is_file()})
    return sorted(p for p in root.rglob('*') if p.is_file() and not any(x in p.parts for x in ['__pycache__','logs','data','state','lab_state','.pytest_cache','node_modules','.git']))
def validate(root):
    errors=[];counts={'python':0,'json':0,'js':0};paths=files(root)
    for group,required in REQUIRED.items():
        for rel in required:
            if not (root/rel).is_file():errors.append(group+':MISSING:'+rel)
    for p in paths:
        rel=p.relative_to(root).as_posix()
        if p.suffix in ['.py','.json','.js','.mjs','.cjs']:
            text=p.read_text(encoding='utf-8-sig')
            try:
                if p.suffix=='.py':ast.parse(text,filename=rel);counts['python']+=1
                elif p.suffix=='.json':json.loads(text);counts['json']+=1
                else:
                    result=subprocess.run(['node','--check',str(p)],capture_output=True,text=True)
                    if result.returncode:raise ValueError(result.stderr)
                    counts['js']+=1
            except Exception as e:errors.append(rel+':'+str(e))
        if p.suffix in ['.js','.mjs']:
            for match in re.finditer(r'\bfrom\s+[\'"](\.[^\'"]+)[\'"]',p.read_text(encoding='utf-8-sig')):
                if not (p.parent/match[1].split('?')[0]).is_file():errors.append(rel+':MISSING_JS_IMPORT:'+match[1])
    tts=json.loads((root/'runtime/core/providers/manifests/tts.json').read_text(encoding='utf-8'))
    for p in tts['providers']:
        for spec in p.get('capabilities',{}).values():
            if not isinstance(spec,dict):continue
            ids=[x['id'] for x in spec.get('allowed_values',[])];assert len(ids)==len(set(ids)),p['id']
    worker=(root/'runtime/higgs/higgs_worker.py').read_text(encoding='utf-8')
    assert 'payload.get("reference_audio_path")' in worker and '_engine.generate(text, options, reference_audio, reference_text)' in worker
    assert 'AUDIO_TRUNCATED' in (root/'runtime/ui/app/audio-router.js').read_text(encoding='utf-8')
    result={'source_map_groups':len(REQUIRED),'files':len(paths),'syntax':counts,'errors':errors,'result':'PASS' if not errors else 'FAIL','FRESH_INSTALL_E2E':'HOLD'}
    print(json.dumps(result,ensure_ascii=False,indent=2));return result
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=ROOT)
    sys.exit(bool(validate(p.parse_args().root.resolve())['errors']))
