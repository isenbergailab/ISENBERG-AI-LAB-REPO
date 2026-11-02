# Separate portable card creation from messaging services

The project defines a platform-neutral Portable Core with configurable Card Roles and Language Profiles. The Hermes Message Adapter, Telegram, Discord, and future services connect through Message Adapters; the initial release promises only the adapters included in its tested Reference Implementation. This avoids coupling extraction and TSV validation to one messaging provider while making “supports any messaging service” an explicit adapter contract rather than an untestable compatibility claim.
