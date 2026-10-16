import json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from runtime.live.adapter import current_persona
class DefaultPersona(unittest.TestCase):
    def make(self,root,ident):
        p=root/'runtime/character/data';p.mkdir(parents=True)
        (p/'active_character.json').write_text(json.dumps({'character_id':ident}),encoding='utf-8')
        with closing(sqlite3.connect(p/'characters.sqlite3')) as c:c.execute('CREATE TABLE character_profile(character_id TEXT)');c.commit()
    def test_legacy_default_without_database_row(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.make(root,'default');p,i=current_persona(root)
            self.assertEqual(i,'default');self.assertIn('中文语音助手',p);self.assertNotIn('伴侣',p)
    def test_missing_named_character_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.make(root,'missing')
            with self.assertRaisesRegex(ValueError,'ACTIVE_CHARACTER_MISSING'):current_persona(root)
if __name__=='__main__':unittest.main()
