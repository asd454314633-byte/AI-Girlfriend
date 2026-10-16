"""Install the documented Apache-2.0 speech-to-speech 0.2.11 overlay in a venv."""
import importlib.metadata,json,shutil,sysconfig
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    if importlib.metadata.version('speech-to-speech')!='0.2.11':
        raise SystemExit('speech-to-speech==0.2.11 required; refusing another wheel')
    site=Path(sysconfig.get_paths()['purelib'])
    overlay=ROOT/'third_party_modified/speech_to_speech_0.2.11'
    for source in overlay.rglob('*.py'):
        target=site/'speech_to_speech'/source.relative_to(overlay)
        target.parent.mkdir(parents=True,exist_ok=True)
        backup=target.with_suffix(target.suffix+'.public-overlay-backup')
        if target.exists() and not backup.exists():shutil.copy2(target,backup)
        shutil.copy2(source,target)
    # Actual hook used by the production pipeline, generated for THIS clone.
    (site/'zero_replica_providers.pth').write_text(str(ROOT/'runtime/core')+'\n'+str(ROOT/'runtime')+'\n'+str(ROOT)+'\n',encoding='utf-8')
    (site/'zz_providers_bootstrap.pth').write_text('import providers.bootstrap as _boot; _boot.install()\n',encoding='utf-8')
    print('S2S_0.2.11_OVERLAY_INSTALLED; pristine replacements backed up once')
if __name__=='__main__':main()
