"""Item 10: "Asked X for Y" becomes a wait and closes the action it replaces, in one proposal."""
import unittest

from helpers import VaultCase, draft

LAB = "02_PROJECTS/DEMO_PROJECT/DEMO_PROJECT.md"
SINGLES = "01_GTD/SINGLE_ACTIONS.md"
CLUB = "03_AREAS/AREA_CLUB.md"


def choose(title, **values):
    """An interpretation that picks the supplied open item with this title as action_id."""
    return lambda text, projects, areas, items: draft(
        action_id=next(i["id"] for i in items if i["title"].startswith(title)), **values)


class DelegateTests(VaultCase):
    remote = True

    def extra_files(self):
        return {CLUB: "---\nkey: AREA_CLUB\ntype: area\naliases: [Club]\nprivate: false\n---\n# AREA_CLUB\n\n"
                      "## Recurring\n"
                      "- [ ] Send the weekly newsletter #computer #next 🔁 every week on Friday 📅 2026-10-02\n"}

    def propose(self, capture, answer):
        self.add_inbox(capture)
        self.provider.interpretations.append(answer)
        found = self.agent.scan().new_proposals
        self.assertEqual(len(found), 1)
        return found[0]["proposal"]

    def test_asked_x_for_y_becomes_a_wait_and_closes_the_action_it_replaces(self):
        before = self.read(LAB)
        proposal = self.propose("Asked Dana to book the room for the lab", choose(
            "Book the room", route="waiting_for", person="Dana", what="Room booking", project_key="DEMO_PROJECT"))
        self.assertEqual(proposal["summary"], "DEMO_PROJECT › Waiting For; closes 'Book the room'")
        self.tick()
        self.agent.sync_review_requests()
        lab = self.read(LAB)
        self.assertIn("- [x] Book the room #calls ✅ 2026-09-28", lab)
        waits = lab[lab.index("## Waiting For"):lab.index("## Log")]
        self.assertIn("- [ ] #waiting Dana: Room booking 📅 2026-10-05 ➕ 2026-09-28", waits)
        self.tick("Undo")
        self.agent.sync_review_requests()
        self.assertEqual(self.read(LAB), before)

    def test_the_wait_lands_where_the_closed_action_lived(self):
        proposal = self.propose("Asked Dana to book the room", choose(
            "Book the room", route="waiting_for", person="Dana", what="Room booking"))
        self.assertEqual([op["path"] for op in proposal["ops"]], [LAB, LAB])
        self.assertEqual(proposal["summary"], "DEMO_PROJECT › Waiting For; closes 'Book the room'")

    def test_a_single_action_passes_its_area_link_to_the_wait(self):
        self.write(SINGLES, self.read(SINGLES).replace(
            "- [ ] Renew library card #computer", "- [ ] Renew library card #computer\n"
            "- [ ] Order the club shirts [[AREA_CLUB]] #computer"))
        proposal = self.propose("Asked Sam to order the club shirts", choose(
            "Order the club shirts", route="waiting_for", person="Sam", what="Club shirts ordered"))
        self.assertEqual(proposal["summary"], "Single actions › Waiting For; closes 'Order the club shirts'")
        self.tick()
        self.agent.sync_review_requests()
        singles = self.read(SINGLES)
        self.assertIn("- [x] Order the club shirts [[AREA_CLUB]] #computer ✅ 2026-09-28", singles)
        self.assertIn("- [ ] #waiting Sam: Club shirts ordered [[AREA_CLUB]] 📅 2026-10-05 ➕ 2026-09-28", singles)

    def test_a_wait_never_closes_another_wait(self):
        proposal = self.propose("waiting on Pat for the room numbers too", choose(
            "Pat: slides", route="waiting_for", person="Pat", what="Room numbers"))  # the model ties it to a wait
        self.assertEqual(proposal["kind"], "waiting_for")  # still filed, where it used to be refused outright
        self.assertEqual([op["op"] for op in proposal["ops"]], ["add_task"])  # and nothing is ticked
        self.assertIn("#waiting Pat: Room numbers", proposal["ops"][0]["line"])
        self.assertIn("your wait 'Pat: slides' stays as it is", proposal["clarification"]["assumptions"])

    def test_a_repeating_duty_is_left_for_the_tasks_plugin(self):
        proposal = self.propose("Asked Kim to send the weekly newsletter", choose(
            "Send the weekly newsletter", route="waiting_for", person="Kim", what="Newsletter sent"))
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("repeats", proposal["reason"])

    def test_a_completed_repeating_duty_is_left_for_the_tasks_plugin(self):
        proposal = self.propose("Sent the weekly newsletter", choose(
            "Send the weekly newsletter", route="completion_report", completion_evidence="Sent the weekly newsletter"))
        self.assertEqual(proposal["kind"], "manual")
        self.assertIn("Tick it in Obsidian", proposal["reason"])


if __name__ == "__main__":
    unittest.main()
