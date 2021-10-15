Heisenbridge with docker-compose
================================

```sh
docker-compose up
```

This is a simplified and automatically self configuring docker-compose example that you can run to test out Heisenbridge.

After the compose setup has successfully started you can head to https://app.element.io and change the homeserver to http://localhost:8008 and register yourself.
Once you have logged in DM `@heisenbridge:localhost` to test connecting to IRC.

For production use it is adviced to generate and handle the registration file manually to a shared host volume that both services can share it on startup without relying on startup scripts.
