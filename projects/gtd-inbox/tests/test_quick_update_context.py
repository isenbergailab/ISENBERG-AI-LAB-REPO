import re
from datetime import date
from helpers import VaultCase, draft
from gtd_agent.clarification import item_candidates
from gtd_agent.vault import Vault

PROJECT = '02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md'

class WaitingApprovalOrderTests(VaultCase):
    def test_first_decision_files_wait_instead_of_resaving_date(self):
        self.add_inbox('#waiting Morgan: attendee estimate [[DEMO_PROJECT]]')
        self.agent.scan()
        review=self.read('_agent/APPROVAL.md')
        # Use the actual decision marker imported below, not item checkboxes.
        from gtd_agent.workflow import DECISION
        tail=review.split(DECISION,1)[1]
        label=re.search(r'^- \[ \] (.+)$',tail,re.M).group(1)
        self.assertEqual(label,'Approve')
        self.tick(label)
        self.assertTrue(self.agent.sync_review_requests()[0].startswith('Applied waiting_for'))
        self.assertNotIn('Morgan',self.read('_agent/APPROVAL.md'))
        self.assertIn('Morgan',self.read(PROJECT))

    def test_existing_date_first_layout_keeps_pending_approval_tick(self):
        self.add_inbox('#waiting Morgan: attendee estimate [[DEMO_PROJECT]]')
        self.agent.scan()
        current=self.read('_agent/APPROVAL.md')
        legacy=current.replace('- [ ] Approve\n- [ ] Save follow-up date',
                               '- [ ] Save follow-up date\n- [x] Approve')
        self.write('_agent/APPROVAL.md',legacy)
        self.agent.render_reviews()
        self.assertIn('- [x] Approve',self.read('_agent/APPROVAL.md'))
        self.assertTrue(self.agent.sync_review_requests()[0].startswith('Applied waiting_for'))

class QuickUpdateContextTests(VaultCase):
    remote=True

    def source(self,status='x'):
        old='- [ ] Email the advisor #computer #next'
        line=f'- [{status}] Send Dana the workshop confirmation #computer ^gtd-source'
        self.write(PROJECT,self.read(PROJECT).replace(old,line))
        return line

    def capture(self):
        return ('Quick update: she replied, the workshop is confirmed for 11/16. '
                'I need to reply to her with attendee numbers by friday. [[DEMO_PROJECT]] '
                '(source: [[02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT#^gtd-source|task]])')

    def test_completed_source_supplies_context_and_followup_without_reclosing(self):
        source=self.source()
        self.add_inbox(self.capture())
        self.provider.interpretations.append(draft(title='Reply to Dana with attendee numbers',
            project_key='DEMO_PROJECT',due_evidence='friday'))
        item=self.agent.scan().new_proposals[0]['proposal']
        call=next(c for name,c in self.provider.calls if name=='interpret')
        self.assertEqual(call.get('source_context',{}).get('title'),'Send Dana the workshop confirmation')
        self.assertEqual(call['source_context']['status'],'done')
        self.assertEqual(call['source_context']['action_id'],'')
        self.assertFalse(any('Send Dana' in candidate['title'] for candidate in call['items']))
        self.assertEqual(item['kind'],'next_action')
        self.assertFalse(any(op['op']=='complete_task' for op in item['ops']))
        self.assertTrue(any(op['op']=='append_log' and '11/16' in op['entry'] for op in item['ops']))
        self.assertIn('📅 2026-10-02',item['ops'][0]['line'])
        self.assertEqual(self.read(PROJECT).count(source),1)
        self.tick();self.agent.sync_review_requests()
        self.assertIn('Reply to Dana with attendee numbers',self.read(PROJECT))
        self.assertIn('11/16',self.read(PROJECT))
        self.assertEqual(self.read(PROJECT).count(source),1)

    def test_explicit_open_source_beats_other_lexical_matches(self):
        self.source(' ')
        text=self.read(PROJECT).replace('## Steps','## Steps\n'+ '\n'.join(
            f'- [ ] Workshop attendee numbers reply {i} #computer' for i in range(12)))
        self.write(PROJECT,text)
        items,_=item_candidates(Vault(self.settings),self.capture())
        self.assertEqual(items[0]['title'],'Send Dana the workshop confirmation')
        self.assertEqual(len(items),8)

    def test_private_source_path_does_not_leak_to_remote_routing(self):
        private='02_PROJECTS/TAXES/TAXES.md'
        self.write(private,self.read(private).replace('Download 1099 forms #computer #next',
            'Download 1099 forms #computer #next ^gtd-private-source'))
        self.add_inbox('Quick update: she replied, send numbers Friday. '
                       '(source: [[02_PROJECTS/TAXES/TAXES#^gtd-private-source|task]])')
        self.provider.interpretations.append(draft(title='Send numbers'))
        result=self.agent.scan().new_proposals[0]['proposal']
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(result['kind'],'manual')

    def test_secret_in_linked_task_blocks_context_before_any_model_call(self):
        self.source()
        self.write(PROJECT,self.read(PROJECT).replace('Send Dana the workshop confirmation',
            'api_key = ' + 'sk' + '-' + 'a' * 22))
        self.add_inbox(self.capture())
        self.provider.interpretations.append(draft(title='Reply to Dana'))
        result=self.agent.scan().new_proposals[0]['proposal']
        self.assertEqual(self.provider.calls,[])
        self.assertEqual(result['kind'],'manual')
        self.assertNotIn('sk-abcdefgh',self.read('_agent/APPROVAL.md'))

    def test_invalid_completion_id_still_waits_for_clarification(self):
        self.source()
        self.add_inbox(self.capture())
        self.provider.interpretations.append(draft(route='completion_report',
            action_id='gtd-source',completion_evidence='she replied'))
        result=self.agent.scan().new_proposals[0]['proposal']
        self.assertEqual(result['kind'],'manual')
        self.assertEqual(result['ops'],[])
