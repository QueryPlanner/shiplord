# Working on Shiplord

Read `ship-cli-plan.md` for the full design and its implementation sequence.
The CLI and server helper are incomplete. Do not claim deployment works.

Use Python 3.11 or later. Install with `python -m pip install -e .` and run
`python -m unittest discover -s tests -v`. Keep the CLI entry point named
`shiplord` and the application manifest named `ship.toml`.

Do not install or test a privileged helper against a production server before
the disposable Ubuntu integration tests described in the plan pass. Do not put
secrets in manifests, output, or tests.
