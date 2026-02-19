# Background

## My Printing Roots

My involvement with printing started at age 17, working as a pressman at
the Plaindealer, a small-town newspaper in Tekamah, Nebraska. From there
I went on to work at several print shops in the Omaha and Council Bluffs
area before moving into prepress at Type House of Iowa in Cedar Falls,
where I operated Linotronic PostScript imagesetters and worked with the
then-new desktop publishing tools like Quark XPress on the Macintosh. It
was this daily hands-on work with PostScript output that gave me a deep
understanding of the language and its role in the printing pipeline.

I've been programming since the Radio Shack Color Computer days, through
the Commodore 64, 128, and Amiga, and into the IBM PC era. The
combination of printing knowledge and programming is what led me to
PostScript development.

## Previous PostScript Work

### PostMaster (1991)

My first PostScript project was PostMaster, a DOS program written in C
that converted PostScript files into Adobe Illustrator 1.1, Adobe
Illustrator 88, Generic EPS, and Data Interchange Format. I self-published
it, complete with professional packaging and 3.5" floppy disks, and sold
about 100 copies. I took it to a couple of software trade shows but never
secured a distribution deal. PostMaster was released before Adobe even
introduced Acrobat and the PDF format.

### Tumbleweed Software

After PostMaster, I wrote a complete PostScript Level 1 interpreter in C
and posted it on CompuServe. It caught the attention of Tumbleweed
Software in Redwood City, California, makers of Envoy — a document format
that competed with Adobe Acrobat and shipped as part of the WordPerfect
Office Suite.

Tumbleweed needed a PostScript front end that could parse and convert
PostScript into the Envoy format. I sold them my interpreter — mostly for
stock — and went to work for the company, spending about three years there
(one year remote, two in Santa Clara). During that time I upgraded the
interpreter to PostScript Level 2 and wrote rasterization code for an HP
project called JetSend, including building the graphics rasterization
pipeline from scratch for Windows 95 (Windows NT already provided the
necessary APIs, but Win95 did not).

I left Tumbleweed after the company went public.

## How PostForge Came About

After a long career detour — including nine years as CTO at Mudd
Advertising — I returned to PostScript in early 2023.

The project started as an experiment. Having written two PostScript
interpreters in C (where a full implementation is typically a 3-5
man-year effort), I was curious whether Python could handle the language's
complex VM semantics — particularly the save/restore memory model. I
actually started in C again, but switched to Python to quickly test some
theories.

It worked. And I just kept going.

In about 45 days of initial work, I had a functional interpreter with
roughly 60,000 lines of code covering the tokenizer, execution engine,
all core operators, the type system, graphics pipeline, VM save/restore,
and both PNG and PDF output. After a two-year hiatus, I picked the
project back up in mid-2025 and have been developing it steadily since.

## Why Python?

I originally chose Python as a rapid prototyping language to test whether
PostScript's VM model was feasible outside of C. It turned out to offer
enormous advantages for interpreter development:

- **No manual memory management** — the biggest time sink in a C
  implementation simply goes away
- **Native dictionaries and lists** — PostScript's core data structures
  map directly to Python's
- **pickle for VM snapshots with copy-on-write optimization** —
  PostScript's save/restore semantics, which require snapshotting the
  entire VM state, map naturally to Python's serialization. A
  copy-on-write layer avoids the cost of full snapshots on every save
  operation
- **Rich library ecosystem** — Cairo for rendering, Pillow for image
  processing and ICC color management, pypdf for PDF construction

The tradeoff is runtime performance, but the optional Cython-compiled
execution loop recovers 15-40% of that gap. For a tool focused on
correctness, debuggability, and readability, Python has proven to be an
excellent fit.

PostForge is my third PostScript interpreter, and by far the most
complete — the first general-purpose implementation rather than a
specialized conversion tool.
