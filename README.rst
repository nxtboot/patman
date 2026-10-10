.. image:: https://raw.githubusercontent.com/nxtboot/patman/master/logo/patman-logo-primary.png
   :alt: Patman
   :width: 400px

Patman patch manager
====================

.. image:: https://github.com/nxtboot/patman/actions/workflows/test.yml/badge.svg
   :target: https://github.com/nxtboot/patman/actions/workflows/test.yml
   :alt: Test status

.. image:: https://img.shields.io/pypi/v/patch-manager.svg
   :target: https://pypi.org/project/patch-manager/
   :alt: PyPI version

.. image:: https://readthedocs.org/projects/patman/badge/?version=latest
   :target: https://patman.readthedocs.io/en/latest/
   :alt: Documentation status

.. image:: https://img.shields.io/pypi/pyversions/patch-manager.svg
   :target: https://pypi.org/project/patch-manager/
   :alt: Supported Python versions

Patman is a command-line tool that automates the boring parts of preparing
and sending patches by email, in the style used by the U-Boot and Linux
kernel projects. It:

- creates patches directly from a git branch;
- cleans them up by removing unwanted tags and inserting a cover letter;
- runs checkpatch.pl and the project's checks before anything is sent;
- looks up the maintainers to Cc, and emails the series out;
- tracks series in a local database and integrates with Patchwork to gather
  review tags and follow up on comments.

Installation
------------

Install the latest release from PyPI::

    pip install patch-manager

Optional features are available as extras::

    pip install patch-manager[gmail]    # send/fetch review mail via Gmail
    pip install patch-manager[review]   # AI-assisted patch review
    pip install patch-manager[all]      # everything

The ``patman`` command is then on your path.

Quick start
-----------

From a git branch containing the commits you want to send::

    patman              # create, check and email the series
    patman -n           # dry run: create and check, but do not send

See ``patman --full-help`` for the complete manual, or the documentation
linked below.

Documentation
-------------

Full documentation is hosted on Read the Docs:

    https://patman.readthedocs.io/

Development
-----------

Run the test suite from a checkout::

    pip install -r requirements.txt
    pip install -e .
    patman test

The suite is self-contained: a pinned ``checkpatch.pl`` and its helper files
live in ``scripts/``, and ``u_boot_pylib`` is installed from PyPI as
``u-boot-pylib``, so no surrounding U-Boot source is required.

Build the documentation locally with::

    pip install -r doc/requirements.txt
    make -C doc html

The rendered HTML is written to ``doc/_build/html``.

License
-------

GPL-2.0-or-later. See the ``LICENSE`` file for the full text.
