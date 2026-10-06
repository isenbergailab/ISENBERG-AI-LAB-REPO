"""One attended synthetic approval demonstration. No network or original vault."""
from datetime import date
from contextlib import ExitStack
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gtd_agent.core import Settings
from gtd_agent.workflow import Agent


def main() -> None:
    with TemporaryDirectory(prefix='gtd-demo-') as temporary, ExitStack() as cleanup:
        base = Path(temporary)
        vault = base / 'vault'
        for source in (ROOT / 'examples/vault').rglob('*'):
            target = vault / source.relative_to(ROOT / 'examples/vault')
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        config = base / 'config.toml'
        config.write_text(f'[vault]\nroot = "{vault.as_posix()}"\n'
                          '[agent]\nmode = "approval"\nstate_dir = "state"\n'
                          'inbox_quiet_seconds = 0\nauto_promote_next = false\ntidy_done_steps = false\n'
                          '[documents]\nenabled = false\n[privacy]\nremote_inference = false\n'
                          '[budget]\nmonthly_usd = 0\n', encoding='utf-8')
        agent = Agent(Settings.load(config), today=date(2026, 10, 5))
        cleanup.callback(logging.shutdown)
        cleanup.callback(agent.close)
        actions = vault / '01_GTD/SINGLE_ACTIONS.md'
        before = actions.read_bytes()
        capture = 'Buy index cards #errands'
        (vault / '00_INBOX/INBOX.md').write_text(capture + '\n', encoding='utf-8')
        result = agent.scan()
        if result.errors:
            raise RuntimeError('Synthetic scan failed: ' + '; '.join(result.errors))
        if actions.read_bytes() != before:
            raise RuntimeError('Destination changed before approval')
        review = vault / '_agent/APPROVAL.md'
        preview = review.read_text(encoding='utf-8')
        if '- [ ] Approve' not in preview:
            raise RuntimeError('Expected synthetic approval missing')
        print('Synthetic capture:', capture)
        print('Destination unchanged before approval.')
        review.write_text(preview.replace('- [ ] Approve', '- [x] Approve', 1), encoding='utf-8')
        outcomes = agent.sync_review_requests()
        if capture not in actions.read_text(encoding='utf-8'):
            raise RuntimeError('Approved capture was not applied')
        print('Explicit demo approval:', '; '.join(outcomes))
        print('Demo complete. Temporary vault removed.')


if __name__ == '__main__':
    main()
