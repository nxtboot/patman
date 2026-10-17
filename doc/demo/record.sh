#!/bin/bash
# SPDX-License-Identifier: GPL-2.0+
#
# Record a short video of patman in a terminal, for the docs and website
#
# Usage: record.sh [U-BOOT-TREE [OUTPUT.webm]]
#
# U-BOOT-TREE is a U-Boot tree (default ~/u) with branches 'binj-us' and
# 'align-fix', which track 'us/master'. A scratch clone of it is made, so the
# tree and its database are not changed, and patman runs from this checkout.
# Nothing is sent: the demo uses 'patman send -n' and reads from patchwork.
#
# This needs Xvfb, xterm, tmux and ffmpeg. It writes OUTPUT.webm, with an
# MP4 copy and a poster image beside it.

set -e
HERE=$(dirname "$(readlink -f "$0")")
PATMAN_DIR=$(readlink -f "$HERE/../..")
TREE=$(readlink -f "${1:-$HOME/u}")
OUT=$(readlink -f "${2:-$PWD/patman-demo.webm}")
DISP=:99
W=1280
H=720
SESS=pdemo

WORK=$(mktemp -d)
cleanup() {
	[ -n "$FF" ] && kill -INT $FF 2>/dev/null && wait $FF 2>/dev/null || true
	tmux -L pdemo kill-session -t $SESS 2>/dev/null || true
	[ -n "$XT" ] && kill $XT 2>/dev/null || true
	[ -n "$XV" ] && kill $XV 2>/dev/null || true
}
trap 'cleanup; [ -d "$WORK" ] && rm -rf "$WORK"' EXIT

# A scratch clone with the two demo branches, tracking us/master
git clone -q --shared --no-checkout "$TREE" "$WORK/u"
cd "$WORK/u"
git remote rename origin local
git remote add us "$(git -C "$TREE" remote get-url us)"
git fetch -q "$TREE" refs/remotes/us/master:refs/remotes/us/master
for branch in binj-us align-fix; do
	git branch -q "$branch" "local/$branch"
	git branch -q -u us/master "$branch"
done
git remote remove local
git checkout -q binj-us
printf '[settings]\npatchwork_url: https://patchwork.ozlabs.org\n' > .patman

# Run patman from this checkout, under its usual name
mkdir "$WORK/bin"
printf '#!/bin/sh\nPYTHONPATH="%s" exec python3 -m patman "$@"\n' \
	"$PATMAN_DIR" > "$WORK/bin/patman"
chmod +x "$WORK/bin/patman"
export PATH="$WORK/bin:$PATH"
patman patchwork set-project U-Boot us >/dev/null
patman upstream add us "$(git remote get-url us)" >/dev/null

Xvfb $DISP -screen 0 ${W}x${H}x24 -nolisten tcp &
XV=$!
sleep 1

tmux -L pdemo -f /dev/null new-session -d -s $SESS -x 140 -y 40 \
	"env GIT_PAGER=cat PATH=$PATH PS1='\[\e[1;32m\]u-boot\[\e[0m\] (\[\e[33m\]\$(git branch --show-current)\[\e[0m\]) \$ ' bash --norc --noprofile"
tmux -L pdemo set -t $SESS status off
tmux -L pdemo send-keys -t $SESS "cd $WORK/u; clear" Enter
sleep 0.5

DISPLAY=$DISP xterm -fa 'DejaVu Sans Mono' -fs 13 -geometry 140x40+0+0 \
	-bg '#1d1f21' -fg '#e0e0e0' -bc -b 12 \
	-xrm 'XTerm*cursorColor: #e0e0e0' -e tmux -L pdemo attach -t $SESS &
XT=$!
sleep 1.5

RAW=$WORK/raw.mkv
ffmpeg -loglevel error -y -f x11grab -draw_mouse 0 -framerate 25 \
	-video_size ${W}x${H} -i $DISP -c:v libx264rgb -crf 0 -preset ultrafast \
	$RAW &
FF=$!
sleep 1

# Type a command a character at a time, run it and wait
run() {
	local cmd=$1 wait=${2:-2}
	local i
	for ((i = 0; i < ${#cmd}; i++)); do
		tmux -L pdemo send-keys -t $SESS -l "${cmd:i:1}"
		sleep 0.03
	done
	sleep 0.4
	tmux -L pdemo send-keys -t $SESS Enter
	sleep "$wait"
}

# Show a comment, as the viewer's guide to what comes next
say() {
	run "# $1" 0.8
}

say "A branch with three commits, ready to send"
run "git log --oneline us/master.." 1.5
say "patman reads its settings from tags in the commits"
run "git log -1 | tail -13" 3.5
run "clear" 0.3
say "Check the series and see who it goes to, without sending"
run "patman send -n" 4
run "clear" 0.3
say "After review, see the new tags on patchwork"
run "git checkout -q align-fix" 0.5
run "patman status" 3
say "Collect them into a new branch, ready for v2"
run "patman status -d align-fix2" 4
run "clear" 0.3
say "Or let patman track all your series"
run "patman series add -M" 1.5
run "patman series ls" 3.5

cleanup
FF=
trap - EXIT
ffmpeg -loglevel error -y -i $RAW -c:v libvpx-vp9 -b:v 0 -crf 30 -row-mt 1 \
	-pix_fmt yuv420p "$OUT"
ffmpeg -loglevel error -y -i $RAW -c:v libx264 -crf 26 -preset slow \
	-pix_fmt yuv420p -movflags +faststart "${OUT%.webm}.mp4"
ffmpeg -loglevel error -y -ss 36 -i $RAW -frames:v 1 "${OUT%.webm}.png"
echo "Wrote $OUT, ${OUT%.webm}.mp4 and ${OUT%.webm}.png"
