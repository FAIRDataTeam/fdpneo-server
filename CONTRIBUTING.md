# Contributing

Thank you for contributing to FAIR Data Point Neo!

## License

This project is licensed under the [MIT License](./LICENSE). By contributing,
you agree that your contributions will be licensed under the same terms.

## Developer Certificate of Origin (DCO)

Every commit must be signed off, certifying the [Developer Certificate of
Origin](./DCO) — i.e., that you have the right to submit the code under the
project's license:

```
git commit -s
```

This appends a `Signed-off-by: Your Name <you@example.org>` trailer using your
git identity. Pull requests with unsigned commits fail CI; fix with:

```
git rebase --signoff HEAD~<n>
git push --force-with-lease
```

## Process

1. Open or comment on an issue before large changes.
2. Fork, branch, and keep PRs focused.
3. Ensure tests and linters pass locally before opening the PR.
