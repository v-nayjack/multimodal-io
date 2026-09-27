# Contributing

Thanks for your interest in improving `@v-nayjack/multimodal-io`!

## How changes get in

```
your fork ──PR──► develop ──(maintainer tests, then PR)──► main ──► release tag
```

-   **`main`** is the stable branch: it's what people install and download.
    It only changes when the maintainer promotes a tested `develop`
-   **`develop`** is where all pull requests go
-   Nobody pushes directly to either branch; every change is a pull request,
    and the maintainer decides what to merge

## Before you start

-   For anything beyond a small fix, **open an issue first** describing the
    problem or idea, so we can agree on the approach before you write code
-   Keep pull requests focused on one change

## Making a change

1.  Fork the repo and create a branch from **`develop`**:

    ```shell
    git clone https://github.com/<you>/multimodal-io.git
    cd multimodal-io
    git checkout develop
    git checkout -b fix/short-description
    ```

2.  Make your change. Match the existing style:
    -   Python: [black](https://github.com/psf/black) with a line length of 79,
        and docstrings for public functions
    -   TypeScript/React: follow the patterns in `src/`

3.  Run the tests (they need a FiftyOne install):

    ```shell
    pytest tests
    ```

4.  If you changed anything in `src/`, rebuild the panel bundle and commit
    `dist/` with your change, since installs use the committed bundle:

    ```shell
    yarn install
    FIFTYONE_DIR=/path/to/fiftyone yarn build
    ```

5.  Test on a FiftyOne Enterprise deployment if you can, and say in the pull
    request what you tested (bucket type, file sizes, App or script)

6.  Open a pull request **into `develop`** and fill in the template

## Never commit

-   Credentials of any kind: API keys, cloud access keys, service account
    files, package index tokens
-   Customer or company data, or real bucket, dataset, and deployment names.
    Use placeholders like `s3://my-bucket/fiftyone` in docs and examples
-   Large test files (`.mcap`, `.mp4`, `.parquet`); `.gitignore` covers these

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same license as the project.
