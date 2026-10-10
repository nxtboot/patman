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

The suite is self-contained: a pinned ``checkpatch.pl`` and its helper
files live in ``scripts/`` and ``u_boot_pylib`` is installed from PyPI as
``u-boot-pylib`` (https://github.com/nxtboot/u-boot-pylib), so no
surrounding U-Boot source is needed. ``checkpatch.pl`` does need ``perl``
to be installed.

Building the documentation
--------------------------

::

    pip install -r doc/requirements.txt
    make -C doc html        # output in doc/_build/html

The manual itself lives in ``patman/patman.rst``; ``doc/`` only wraps it
for Sphinx, so edit the manual there.

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
