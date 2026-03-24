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

✨ Effortlessly create versions or restore previous states of your project data.
<br>🪙 Available exclusively for **Pro** and <span style="color:#96f">**Enterprise**</span> subscribers.

## How to Run

The application works in two modes: **Create** and **Restore**. Depending on the selected action, the application is launched within the project under the Versions tab.

### **Create Mode**

In Create mode, you can manually specify the **Name** and **Description** of the version, which helps in identifying the state of the project at the time the version was created. <br>Some applications can create versions automatically, using their own **Name** and **Description** that make them easily recognizable.

#### **Enable Preview**

`Available for image and video project types only`

When creating a version, you can enable the **Version Preview** option. A Preview is a read-only version with quick access that uses the project panel interface — it allows you to inspect the project's state at the time the version was created. From the preview, you can also open the labeling tool to view annotations for each image or video. All editing is disabled in preview mode — all controls are inactive, and no entities can be modified.

### **Restore Mode**

In Restore mode, you need to specify the **Version** number from which you want to create a new project. The state of this new project will match that version.
