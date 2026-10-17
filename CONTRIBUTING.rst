Contributing to Patman
======================

Development setup
-----------------

Work in a virtual environment::

    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    pip install -e .[test]

Running the tests
-----------------

::

    patman test                 # the whole suite
    patman test <name>          # a single test, e.g. test_series_send

The suite can also be run with ``pytest``.

The suite is self-contained: a pinned ``checkpatch.pl`` and its helper
files live in ``scripts/`` and ``u_boot_pylib`` is installed from PyPI as
``u-boot-pylib`` (https://github.com/deinde-dev/u-boot-pylib), so no
surrounding U-Boot source is needed. ``checkpatch.pl`` does need ``perl``
to be installed.

Building the documentation
--------------------------

::

    pip install -r doc/requirements.txt
    make -C doc html        # output in doc/_build/html

The manual lives in ``patman/``: the introduction in ``patman.rst``, then
one file for each chapter, such as ``sending.rst`` and ``tags.rst``.
``control.HELP_FILES`` lists them in order. ``patman -H`` shows them all, and
``doc/`` wraps them for Sphinx, so edit the manual there. To link to another
page, use a :doc: link with its own text, such as
``:doc:`the Tags chapter </tags>```, since ``patman -H`` shows just the text.

The command reference in ``doc/reference/`` is generated from the argument
parser by ``doc/_ext/patman_cmdref.py``, so give each new command and option
a help string. ``doc/guide/`` holds the step-by-step guide, with output from
real series.

The technical documentation is in ``doc/`` too. ``doc/database.rst``
describes the database schema; update it along with any schema migration.
``doc/serve-protocol.rst`` describes the JSON API provided by
``patman serve``; update it along with any change to the API.
``doc/review-internals.rst`` describes how the review works.

``doc/demo/record.sh`` records the short video of patman in a terminal which
is shown at https://deinde.dev/patman/. Run it again after a change to the
output which the video shows.

Building the package
--------------------

::

    python -m build
    twine check --strict dist/*

Continuous integration
----------------------

The *Tests* workflow runs the suite on Python 3.10-3.12 and builds and
checks the package on every push and pull request.

Making a release
----------------

Releases are published to PyPI automatically by the *Release* workflow
when a version tag is pushed. The flow is:

1. Update ``CHANGELOG.rst``: move the ``Unreleased`` entries under a new
   ``X.Y.Z - <date>`` heading.
2. Bump ``version`` in ``pyproject.toml``.
3. Commit the changes.
4. Tag it. The tag must be ``v`` followed by the exact version, for
   example::

       git tag v0.0.8
       git push origin v0.0.8

   A tag containing ``rc`` (for example ``v0.0.8rc1``) publishes to
   TestPyPI; a final tag publishes to the real PyPI. The workflow
   refuses to publish if the tag does not match the project version.

   You can also trigger the *Release* workflow manually (the
   ``workflow_dispatch`` option) to publish the current branch to
   TestPyPI for a dry run, without a tag.

Publishing uses PyPI Trusted Publishing (OIDC), so no API tokens are
stored. The PyPI and TestPyPI projects must each be configured to trust
this repository's ``release.yml`` workflow (environments ``pypi`` and
``testpypi``) before the first release.
