from datetime import datetime
from helpers import VaultCase,TODAY,draft
from tasks_query import queries,select
from gtd_agent.housekeeping import Housekeeper
import test_quick_update_context as context_tests

class ProjectViewTests(VaultCase):
    def refresh(self):
        house=Housekeeper(self.agent);vault=self.agent.vault()
        house.refresh_next_actions(vault)
        house.digest(vault,datetime(2026,9,28,9,0))
    def shown(self,path,heading):
        return {t.title for t in select(queries(self.read(path))[heading],self.agent.vault().tasks,TODAY)}
    def test_queue_visible_in_both_views_without_promoting_steps(self):
        self.refresh()
        for rel in ['_agent/TODAY.md','01_GTD/NEXT_ACTIONS.md']:
            self.assertIn('Book the room',self.shown(rel,'Queued project steps'))
        self.assertIn('- [ ] Book the room #calls\n',self.read('02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md'))
    def test_new_projects_and_parked_subnotes_refresh_both_views(self):
        self.refresh()
        rel='02_PROJECTS/NEW_PROJECT/NEW_PROJECT.md'
        self.path(rel).parent.mkdir(parents=True)
        self.write(rel,'---\nkey: NEW_PROJECT\ntype: project\nstatus: active\n---\n# NEW_PROJECT\n## Steps\n- [ ] New first action #computer #next\n- [ ] New queued action #calls\n')
        self.write('02_PROJECTS/NEW_PROJECT/notes.md','# Notes\n- [ ] Subnote action #computer #next\n')
        self.refresh()
        self.assertIn('New queued action',self.shown('01_GTD/NEXT_ACTIONS.md','Queued project steps'))
        self.assertIn('Subnote action',self.shown('01_GTD/NEXT_ACTIONS.md','@computer'))
        for status in ['someday','done','dropped']:
            self.write(rel,self.read(rel).replace('status: active','status: '+status))
            self.refresh()
            for view in ['_agent/TODAY.md','01_GTD/NEXT_ACTIONS.md']:
                tasks=[t for q in queries(self.read(view)).values() for t in select(q,self.agent.vault().tasks,TODAY)]
                self.assertFalse(any(t.path.startswith('02_PROJECTS/NEW_PROJECT/') for t in tasks))
            self.write(rel,self.read(rel).replace('status: '+status,'status: active'))
        self.refresh()
        self.assertIn('New first action',self.shown('01_GTD/NEXT_ACTIONS.md','@computer'))
    def test_future_starts_stay_scheduled_and_custom_notes_survive(self):
        self.write('01_GTD/NEXT_ACTIONS.md','# NEXT_ACTIONS\n\nPersonal introduction.\n\n## My notes\nKeep this paragraph.\n')
        rel='02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md'
        self.write(rel,self.read(rel).replace('Book the room #calls','Book the room #calls 🛫 2027-02-12'))
        self.refresh()
        for view in ['_agent/TODAY.md','01_GTD/NEXT_ACTIONS.md']:
            self.assertNotIn('Book the room',self.shown(view,'Queued project steps'))
        saved=self.read('01_GTD/NEXT_ACTIONS.md')
        self.assertIn('Personal introduction.',saved);self.assertIn('Keep this paragraph.',saved)
        self.refresh();self.assertEqual(saved,self.read('01_GTD/NEXT_ACTIONS.md'))

class QuickUpdateMissingExplanationTests(VaultCase):
    remote=True
    source=context_tests.QuickUpdateContextTests.source
    capture=context_tests.QuickUpdateContextTests.capture
    def test_missing_model_explanation_is_repaired_once(self):
        self.source();self.add_inbox(self.capture())
        self.provider.interpretations.extend([draft(title='Reply to Dana',project_key='DEMO_PROJECT',explanation='',lesson=''),draft(title='Reply to Dana with attendee numbers',project_key='DEMO_PROJECT',due_evidence='friday')])
        proposal=self.agent.scan().new_proposals[0]['proposal']
        self.assertEqual(proposal['kind'],'next_action')
        calls=[args for name,args in self.provider.calls if name=='interpret'];self.assertEqual(len(calls),2)
        self.assertIn('explanation and a lesson',calls[-1]['issue'])
    def test_second_missing_explanation_still_stops(self):
        self.source();self.add_inbox(self.capture())
        self.provider.interpretations.extend([draft(title='Reply to Dana',explanation='',lesson='') for _ in range(2)])
        proposal=self.agent.scan().new_proposals[0]['proposal'];self.assertEqual(proposal['kind'],'manual')
        self.assertEqual(len([1 for name,_ in self.provider.calls if name=='interpret']),2)


class StructuredProjectBriefTests(VaultCase):
    def test_explicit_checklist_beats_model_omissions_and_keeps_dates_waits_and_tags(self):
        from gtd_agent.documents import DocumentIntake
        text = "# Brief\n## Steps\n- [ ] First task #computer #next #15m 🛫 2026-10-01 📅 2026-10-03 ➕ 2026-09-20\n- [ ] Second task #calls #next\n- [ ] #waiting Morgan: confirmation 📅 2026-10-05 ➕ 2026-09-20\n- [x] Already finished #computer ✅ 2026-09-21\n"
        source='00_INBOX/NEW_PROJECT.md';self.write(source,text)
        clean={'folder':'','title':'Brief','doc_type':'notes','summary':'Summary','facts':[],
               'deadlines':[],'actions':[], 'explanation':'No model tasks returned.','notes':[]}
        intake=DocumentIntake(Housekeeper(self.agent))
        proposal=intake.compile_new_project(self.agent.vault(),source,'NEW_PROJECT.md',text,
            self.path(source).read_bytes().hex(),clean,TODAY,{},'NEW_PROJECT',None)
        taskops=[o for o in proposal['ops'] if o['op']=='add_task']
        self.assertEqual(len(taskops),3)
        self.assertIn('#next #15m 🛫 2026-10-01 📅 2026-10-03 ➕ 2026-09-20',taskops[0]['line'])
        self.assertIn('#calls #next',taskops[1]['line'])
        self.assertEqual(taskops[2]['section'],'Waiting For')
        self.assertIn('#waiting Morgan: confirmation 📅 2026-10-05',taskops[2]['line'])
        self.assertFalse(any('Already finished' in o['line'] for o in taskops))
