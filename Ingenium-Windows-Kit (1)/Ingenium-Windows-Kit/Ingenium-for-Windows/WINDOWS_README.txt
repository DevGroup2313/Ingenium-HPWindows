INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
NO LICENCE IS GRANTED. See LICENCE.txt. Every distributed copy carries a
build marker.
--------------------------------------------------------------------------

Ingenium for Windows
=======================

Install it (recommended)
------------------------
Ingenium-Setup.exe is a normal Windows installer. It needs nothing on the
PC: no Python and no administrator password. It works on Windows 10 and 11.

  1. Download Ingenium-Setup.exe from the Releases page. If Edge says the
     file is not commonly downloaded, choose Keep.
  2. Double-click it. If Windows SmartScreen says "Windows protected your
     PC", choose More info, then Run anyway: the installer has not been
     through a paid signing process.
  3. It installs at once, with no questions, and ends with "Open Ingenium
     now" ticked. Press Finish.

Ingenium then sits on the Desktop and in the Start menu with its own icon,
and Settings, Apps lists it, so it can be removed there like any program.
It opens at once: it is installed as a folder, so it never unpacks itself,
and its engine is already in place beside it. The first time it opens, it
offers to download its two models (about 2 GB, once; a broken download
carries on where it stopped).

To update, run a newer Ingenium-Setup.exe; an open Ingenium is closed
first. Your chats, notes, settings and models are never touched: they live
in AppData\Local\Ingenium and AppData\Roaming\Ingenium, outside the program
folder, and removing the app leaves them too. If the earlier Python version
of Ingenium is on the PC, the installer replaces it in the same folder and
points its shortcuts at the new app; nothing of yours is lost.

For a PC without internet, Ingenium-Windows-with-models.zip holds the same
installer with a models folder beside it: unzip it and double-click
Ingenium-Setup.exe inside, and the models install too. If anything goes
wrong, the installer leaves a log named "Setup Log" in the temporary folder
(type %TEMP% in the File Explorer address bar).


Other ways in
=============

1. Just open it (no build, works now)
-------------------------------------
Double-click ingenium.html. It opens in your browser (Edge or Chrome) and
runs completely offline. That single file is the entire application, and its
engine is inside it: go to the Models page, add a .gguf model file, and press
Load.

What was fixed (R64): opened by double-clicking, or in the Windows app's
window, the engine used to stop at "Loading model 95%" for ever, because the
browser refused to start its worker from a file. It now starts, and if
anything ever stops it, the load ends with a reason instead of waiting.

For an app-like window with the Ingenium icon, without building anything:
  1. Open ingenium.html in Microsoft Edge.
  2. Click the "..." menu, then Apps, then "Install this site as an app".
It then behaves like a native app with its own window and icon, and works
offline afterwards.


2. Build it as a proper Windows app (Ingenium.exe)
-----------------------------------------------------
This gives you Ingenium.exe with its own window and the Ingenium icon, no
browser around it, its OWN ENGINE (llama.cpp's own build for Windows, many
times faster than the engine inside the page, and able to open model files
of any size) and two BUILT-IN MODELS (LFM2.5 1.2B Instruct, quick, and Qwen3.5
2B, which thinks before it answers), so it is useful the moment it
opens. If the PC has a graphics chip that supports Vulkan (most do, with up
to date drivers), the engine uses it as well as the processor.

  1. Check you have Python. Open Command Prompt and type:  py --version
     If it is missing, install Python 3.12 or 3.13 from
     https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  2. Double-click "Build Ingenium (Windows).bat". (If a built app ever
     misbehaves, "Run Ingenium (Windows).bat" opens the same app without
     building and prints any fault live.)
  3. Watch the window: every step prints as it works -- the tools (once),
     the engine (about 40 MB, once), the exe, the built-in models (about
     2 GB together, once), and a test that the app's own engine answers a
     question and that the second model thinks.
     Nothing sits silently, and if a step cannot be done the window says why
     in plain words instead of looking stuck.
  4. When it finishes, the app is installed in your user folder
     (AppData\Local\Programs\Ingenium: Ingenium.exe and its models folder)
     with a shortcut on your Desktop. Open it from the shortcut.

This build on your own PC uses llama.cpp's published Windows engine, which
has no trainer, so "Train a small model from zero" (Models page) is missing
from it. The installer and app that GitHub builds (section 3) carry the
trainer, which GitHub compiles from the same llama.cpp release.

The build makes a private .buildenv folder for its own tools, so it cannot
collide with any Python setup you already have, and everything downloaded is
reused on later runs. You can delete .buildenv, build, dist and
build_log.txt afterwards. If a run ever seems stuck on a slow USB stick, copy
the whole folder to your Desktop first and run it there.


3. Or let GitHub build it
-------------------------
The repository builds the Windows app by itself whenever its files change
(.github/workflows/build-windows.yml): it gets the engine, checks it, builds
Ingenium.exe, adds the built-in models and TESTS the app: its own engine must
answer "What is the capital of France?", and the thinking model must think,
before anything is published. The
finished app appears on the repository's Releases page:

  Ingenium-Setup.exe                the installer: see "Install it" at the
                                    top.
  Ingenium-Windows-with-models.zip  the same installer with the two models
                                    beside it (about 2 GB), for a PC without
                                    internet.
  Ingenium-Windows-portable.zip     Ingenium.exe alone, one file that needs
                                    no installing. It opens more slowly,
                                    because it unpacks itself each time.

Before publishing, the build installs Ingenium-Setup.exe on GitHub's
Windows machine over what the earlier Python version left, tests the
installed app the same way, times how long it and the portable exe each
take to start, runs the installer again as an update, and removes the app
through its uninstaller, checking each time that the user's own files are
kept (the summary of each build shows the results).

Keep this repository private: only you should see its files and its
Releases page. On a private repository, GitHub counts each minute on its
Windows machines twice against the free monthly allowance (2,000 minutes on
a free account). A build takes about 8 minutes (it builds, installs, tests,
updates and removes the app), so it uses about 16 of them.
These are GitHub's terms at the time of writing and may change; the billing
section of your GitHub account settings shows the current ones.


Your models on every device
---------------------------
A computer running Ingenium, such as a Mac mini, can lend its stronger models
to this PC over your home Wi-Fi, and this PC can lend its own to your other
devices. Settings, "Your models on every device":
  * To use another computer's models: enter the address and the pairing code
    that computer shows when sharing is on, and press Connect.
  * To lend this PC's models: switch on "Share this computer's models", then
    enter the address and code it shows on the other device. If Windows
    Firewall asks, allow Ingenium on private networks. While
    sharing is on, the PC does not go to sleep by itself.
Nothing leaves your home network, and only devices with the code are
answered. If the name does not work, use the number address it shows.


Adding models
-------------
On the Models page, click the box that says "Drop a model file here, or click
to choose". Windows' own file window opens. Choose one or more .gguf files.
Ingenium links each file where it already is: nothing is copied, so even a
large file is ready at once, and removing a model from Ingenium never deletes
the file. If Ollama is installed, the same page offers the models Ollama has
already downloaded, linked the same way. The size limit is your PC's memory:
as a rough guide (an estimate), a model needs about its file size in memory
plus a little more for its Memory setting. The Models page shows whether
each model is likely to fit.

Speed-up files: when the engine runs on this PC's graphics chip, the Models
page offers "Add speed-up" for Velox (LFM2.5 1.2B Instruct) and the LFM2.5
2.6B house: their maker's small draft model (Liquid AI's DSpark, about 170
to 190 MB, checked against its SHA-256). The engine then guesses a few words
ahead and checks them in one step. On a Mac's graphics chip this made
replies about half to two thirds faster in two of Ingenium's cloud tests;
on GitHub's Windows machine, which runs models on its processor, it made
them about a quarter slower, so it is offered only where a graphics chip
runs the model. No Windows graphics chip has been tested with it yet.

Train a small model from zero (Models page): a guided lesson that makes a
tiny model of your own, with random numbers at first, and trains it on text
you give it, on this PC, with llama.cpp's trainer. It runs on the processor.
Minutes of training teach a tiny model the common words and punctuation of
your text, then short phrases; it does not hold a conversation. The models
you chat with were trained on vastly more text and computing.


If a window will not open
-------------------------
Ingenium's window on Windows uses the Microsoft Edge WebView2 runtime.
It ships with Windows 11 and with updated Windows 10. If it is missing, the
app says so. To get the window, install "Microsoft Edge WebView2 Runtime"
(Evergreen Standalone Installer) from Microsoft's site, then run the app
again.

If SmartScreen warns "Windows protected your PC" the first time you run the
exe, that is the same kind of check as macOS Gatekeeper: it appears for any
program that has not been through a paid code-signing and reputation process.
Click "More info", then "Run anyway". You never need the exe anyway:
ingenium.html is the whole app.


What the app connects to
------------------------
Nothing, by itself. It connects only when you turn a connection on in
Settings (Ollama on another computer, or a cloud provider such as Claude).
The app's own engine listens only on this PC (a loopback address nothing
outside the PC can reach) and refuses any request without the app's private
key. The build step is different: it downloads its tools, the engine and the
built-in model once, then keeps them.
