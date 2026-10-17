.. SPDX-License-Identifier: GPL-2.0+

Guide
=====

These pages show how to use patman, step by step, with a real U-Boot series:
three binman documentation patches, ``binj-us``, which went to two versions
and were merged in v2026.07. A second small series, ``align-fix``, shows
collecting review tags, and a third, ``typos``, sending to the concept tree.
The last pages review a series from the concept tree. All the output is from
running patman on these branches.

Read them in order the first time. Each one follows on from the one before.

:doc:`getting-started`
    Install patman, set up git and ``git send-email`` (or the kernel.org web
    relay), the U-Boot tree and ``~/.patman``

:doc:`first-series`
    Turn a branch of commits into a series: the tags in the top commit, a
    dry run, the patches and cover letter it creates, and sending it

:doc:`status-and-tags`
    Link the branch to patchwork, then use ``patman status`` to see the
    reviews, read the comments and collect new tags into a branch

:doc:`next-version`
    Send v2: a branch for the new version, change logs in each commit, adding
    a patch, and how the change logs appear in the patches and cover letter

:doc:`series-workflow`
    Let the ``patman series`` database keep track of it all: finding the
    patchwork links, gathering tags, starting each version and archiving the
    series once merged

:doc:`upstreams`
    Send to more than one tree, such as mainline and the concept tree, each
    with its own mailing list, patchwork server and send settings

:doc:`reviewing`
    Review someone else's series with a Claude agent: apply it, review each
    patch, change a review with a note, and send the reviews

:doc:`serve`
    Follow your series and reviews from a phone with ``patman serve``, and
    work on the reviews there

:doc:`problems`
    Messages you may see along the way, what causes them and what to do

The :doc:`/reference/index` lists every command and its options. The manual
covers each command and tag in full, along with features not shown here,
such as syncing Gmail drafts and responding to replies to your reviews.

.. toctree::
   :hidden:

   getting-started
   first-series
   status-and-tags
   next-version
   series-workflow
   upstreams
   reviewing
   serve
   problems
