<h1> Deploy with Docker </h1>

<h2>Table of Contents</h2>

- [Introduction](#introduction)
- [Deployment Steps](#deployment-steps)
- [Clean Up](#clean-up)

---

## Introduction

This folder contains the files necessary to deploy the node status bot with Docker.

## Deployment Steps

Here are the basic steps to deploy the node status bot with Docker

```
# Export the Telegram bot token
export BOT_TOKEN=1234ABC 

# Deploy the containers in the background
docker compose up -d

# Verify the containers are running
docker ps
```

Note that you can also set the bot token in an `.env` file.

## Clean Up

To stop the node status bot simply run:

```
docker compose down
```