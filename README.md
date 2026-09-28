<div align='center' markdown> 
<img src="https://github.com/supervisely-ecosystem/data-versioning/assets/57998637/acad95dc-abb3-4407-bbaf-2db0734ba126" /> <br>

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

✨ Effortlessly create versions, compare them, or restore previous states of your project data.
<br>🪙 Available exclusively for **Pro** and <span style="color:#96f">**Enterprise**</span> subscribers.

## How to Run

The application works in three modes: **Create**, **Compare** and **Restore**. Depending on the selected action, the application is launched within the project under the Versions tab.

### **Create Mode**

In Create mode, you can manually specify the **Name** and **Description** of the version, which helps in identifying the state of the project at the time the version was created. <br>Some applications can create versions automatically, using their own **Name** and **Description** that make them easily recognizable.

#### **Enable Preview**

`Available for image and video project types only`

When creating a version, you can enable the **Version Preview** option. A Preview is a read-only version with quick access that uses the project panel interface — it allows you to inspect the project's state at the time the version was created. From the preview, you can also open the labeling tool to view annotations for each image or video. All editing is disabled in preview mode — all controls are inactive, and no entities can be modified.

### **Compare Mode**

Compare answers one question: **what changed between two versions of the project**. Open the **Versions** tab, pick two versions, and press **Compare**.

The comparison names changes rather than showing them — there are no side-by-side pictures. It reads the two versions and reports:

- which items were **added**, **removed**, **renamed**, **moved**, or had their **annotation changed**;
- inside a changed item, which objects, figures and tags were added, removed or edited — down to the frame of a video or the slice of a volume;
- what happened to the project's own definitions: its classes, tags and settings.

The report opens on its own page, where you can filter the list by kind of change and open any item to see what happened inside it. **Download HTML** saves the whole report as a single file that opens in any browser, with no connection to the instance.

A comparison of two committed versions can never change, so it is computed once and reused: opening the same pair again is instant. Two different pairs of the same project can be compared at the same time, each on its own agent.

Some pairs cannot be compared, and the app says which and why. A version created before the columnar snapshot format has no annotation tables to read, and some older versions recorded annotations without the ids needed to tell one figure from another across versions. Create a fresh version and compare from there.

### **Restore Mode**

In Restore mode, you need to specify the **Version** number from which you want to create a new project. The state of this new project will match that version.
