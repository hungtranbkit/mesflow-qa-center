# QA Center v1.22.3 — Workspace-only installer

`install.sh` at the release root now has one responsibility: copy this QA Center project source into the standard MESFlow source workspace.

Default destination:

```text
~/workspace/mesflow/qa-center
```

When invoked with `sudo`, the installer resolves the original `SUDO_USER` home so it does not accidentally install into `/root/workspace`.

The installer preserves local-only `.env`, `.env.local`, and `config.local.json` when present, and excludes runtime/build residue such as `runtime/`, reports, logs, backups, evidence, node_modules, virtualenvs and caches.

It intentionally does **not** call Docker, Compose, systemd, migrations, or any deploy command. Build/deploy is owned by Deploy Agent / ProjectFlow after source lands in the workspace.
