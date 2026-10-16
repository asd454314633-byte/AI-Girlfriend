"""Initialize only empty public-deployment settings. Never copy user databases."""
import argparse,json,secrets,sys,sysconfig,os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def write_once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():return
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def initialize(root):
    root=Path(root).resolve()
    models=json.loads((root/'config/models.example.json').read_text(encoding='utf-8'))
    models['llama_server']=str(root/models['llama_server'])
    for model in models['models'].values():model['path']=str(root/model['path'])
    write_once(root/'runtime/llm/models.json',models)
    write_once(root/'runtime/llm/active_model.json',{'active_model':'dark_roast'})
    live=json.loads((root/'config/live.example.json').read_text(encoding='utf-8'))
    live['bridge_path']=str(root/live['bridge_path'])
    write_once(root/'runtime/live/config.json',live)
    write_once(root/'runtime/character/data/active_character.json',{'character_id':'default'})
    for folder in ['logs/runtime','reading/library','reading/index','runtime/windows','runtime/reading/data','runtime/voices/profiles','runtime/voices/trash','runtime/higgs','models/native/qwen3','models/llm','tools/llama.cpp']:
        (root/folder).mkdir(parents=True,exist_ok=True)
    key=root/'runtime/higgs/higgs_api_key.txt'
    if not key.exists():key.write_text(secrets.token_hex(32),encoding='utf-8')
    # Initialize schemas only. No user profile, memory or reading rows are seeded.
    sys.path[:0]=[str(root),str(root/'runtime'),str(root/'runtime/core')]
    from runtime.character import store
    from runtime.memory import service
    for path,initializer in [(root/'runtime/character/data/characters.sqlite3',store.init_db),
                             (root/'runtime/memory/data/memories.sqlite3',service.init_db)]:
        if not path.exists():initializer(path)
    reading_db=root/'runtime/reading/data/reading.sqlite3'
    if not reading_db.exists():
        os.environ['GIRLFRIEND_READING_DB']=str(reading_db)
        os.environ['READING_ROOT']=str(root/'reading')
        from reading import documents
        documents._db().close()
    # Native UI manager generates separate owner-only HMAC state at first start.
    print('INITIALIZED_EMPTY_SETTINGS (no models, user media or records)')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=ROOT)
    initialize(p.parse_args().root)
