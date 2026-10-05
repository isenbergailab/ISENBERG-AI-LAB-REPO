"""Configured organization screening protects mail without contributor-specific rules."""
from helpers import VaultCase
from gtd_agent.screen import screen


class RestrictedDomainTests(VaultCase):
    def extra_config(self):
        return '[mail]\nrestricted_domains = ["Restricted.Example"]\n'

    def test_configured_domain_blocks_embedded_and_sender_addresses_for_self_mail(self):
        self.assertEqual(screen(['Ask analyst@dept.restricted.example'], [], self.settings, categories=False), 'restricted work')
        self.assertEqual(screen(['Ordinary text'], ['analyst@restricted.example'], self.settings, categories=False), 'restricted work')
        self.assertEqual(self.settings.mail_restricted_domains, ('restricted.example',))

    def test_domain_boundaries_preserve_unrelated_synthetic_mail(self):
        self.assertIsNone(screen(['Ask analyst@notrestricted.example'], [], self.settings, categories=False))
        self.assertIsNone(screen(['Ask analyst@restricted.example.org'], [], self.settings, categories=False))

    def test_an_empty_local_list_adds_no_organization_rule(self):
        from dataclasses import replace
        settings = replace(self.settings, mail_restricted_domains=())
        self.assertIsNone(screen(['Ask analyst@restricted.example'], [], settings, categories=False))

    def test_invalid_domain_configuration_fails_closed(self):
        from gtd_agent.core import Settings
        for invalid in ('analyst@restricted.example', 'https://restricted.example', ''):
            text = self.settings.config_path.read_text(encoding='utf-8')
            text = text.replace('["Restricted.Example"]', f'["{invalid}"]')
            self.settings.config_path.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError):
                Settings.load(self.settings.config_path)
            self.settings.config_path.write_text(text.replace(f'["{invalid}"]', '["Restricted.Example"]'), encoding='utf-8')
