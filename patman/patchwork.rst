.. SPDX-License-Identifier: GPL-2.0+

Patchwork
=========

Patchwork collects the patches sent to a mailing list, along with the
replies and review tags they receive. patman uses it to find your series once
it is sent, to show what reviews it has received, and to collect the review
tags into your commits.

Setting up the project
----------------------

patman uses the patchwork server given by the ``patchwork_url`` setting (see
:doc:`the Configuration chapter </configuration>`) or ``-P``, or the server of
the series' upstream, if it has one. U-Boot's ``.patman-defaults`` sets
https://patchwork.ozlabs.org

Commands which search patchwork for a series, such as
``patman series autolink``, also need to know its project. Set this once for
each remote::

    $ patman patchwork set-project U-Boot us
    Project 'U-Boot' patchwork-ID 18 link-name 'uboot' remote 'us'

The remote can be left out if the current branch tracks it, or if it is the
only remote. ``patman patchwork get-project`` shows the project for a remote,
``ls`` lists the projects and ``rm`` removes one::

    $ patman patchwork ls
    Project                 ID  Link name        Remotes
    --------------------  ----  ---------------  ---------------
    U-Boot                  18  uboot            us
    --------------------  ----  ---------------  ---------------

    $ patman patchwork rm us
    Deleted patchwork project for upstream 'us'

With no remote, ``rm`` removes the project which has no upstream.

Checking the review status
--------------------------

Once your series is on patchwork, patman can show you what new reviews have
appeared since you sent it.

To set this up, add a Series-links tag to one of the commits in your series
(see :doc:`the Tags chapter </tags>`).

Then you can type:

.. code-block:: bash

    patman status

and patman will show you each patch and what review tags have been collected,
for example::

    ...
     21 x86: mtrr: Update the command to use the new mtrr
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
     22 x86: mtrr: Restructure so command execution is in
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
    ...

This shows that patch 21 and 22 were sent out with one review but have since
attracted another review each. If the series needs changes, you can update
these commits with the new review tag before sending the next version of the
series.

There is also a -c option to list the comments received for each patch, and a
-C option to list those on the cover letter.

Collecting the tags
-------------------

To automatically pull these tags into a new branch, use the -d option:

.. code-block:: bash

    patman status -d mtrr4

This will create a new 'mtrr4' branch which is the same as your current branch
but has the new review tags in it. The tags are added in alphabetic order and
are placed immediately after any existing ack/review/test/fixes tags, or at the
end. A tag given in a reply to the cover letter, such as a Reviewed-by for the
whole series, is added to every patch which does not already have it. The new branch does not track an upstream branch, so set one with
``git branch --set-upstream-to`` before sending from it.

For a worked example using real series, see
:doc:`Checking status and collecting tags </guide/status-and-tags>`.

Tracking several series
-----------------------

If you have several series in flight, 'patman series' keeps track of them for
you in a database: each version, its patchwork link and the state of each
patch. It can find the links, gather the review tags, start each new version
and archive a series once merged. The
:doc:`Series management chapter </series>` describes this.
