<div align='center' markdown> 
<img src="https://github.com/supervisely-ecosystem/sys-archiver-media/assets/57998637/aab35196-4c53-40d8-b53d-15969ff6f169" /> <br>

<p align='center'>
  <a href='#overview'>Overview</a> •
  <a href='#how-to-run'>How To Run</a>
</p>

[![](https://img.shields.io/badge/supervisely-ecosystem-brightgreen)](https://ecosystem.supervisely.com/apps/supervisely-ecosystem/data-versioning)
[![](https://img.shields.io/badge/slack-chat-green.svg?logo=slack)](https://supervisely.com/slack)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/supervisely-ecosystem/data-versioning)
[![views](https://app.supervisely.com/img/badges/views/supervisely-ecosystem/data-versioning.png)](https://supervisely.com)
[![runs](https://app.supervisely.com/img/badges/runs/supervisely-ecosystem/data-versioning.png)](https://supervisely.com)

</div>

## Overview

Easily create versions or restore previous state of your projects data.

## How to Run

The application has two modes: Create and Restore.

### Create Mode

In Create mode, you can manually specify the **Title** and **Description** of the version, which helps in identifying the state of the project at the time the version was created. <br>Some applications can create versions automatically, using their own **Title** and **Description** that make them easily recognizable.

### Restore Mode
In Restore mode, you need to specify the **Version** number from which you want to create a new project. The state of this new project will match that version. <br>An additional parameter, **Skip Missed Images**, allows you to create a new project even if some images are permanently lost (e.g., they were deleted or are inaccessible in remote storage).

### Pro Tips
**Local Images:** If you still have the images locally that were previously on the server, you can upload them to a new empty project. This allows you to fully recreate the **Version** as you will reestablish the connection to these images via their hashes.

**Links:** Handling links is more challenging because you'll need to manually update them to new links within the backup objects. In this case, you can use the SDK to assist with these tasks.