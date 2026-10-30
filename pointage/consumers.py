import json
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken


class AttendanceConsumer(AsyncWebsocketConsumer):
    """Live refresh pings for dashboards; requires a valid JWT in `?token=`."""

    async def connect(self):
        token = parse_qs(self.scope.get("query_string", b"").decode()).get("token", [""])[0]
        try:
            AccessToken(token)
        except TokenError:
            await self.close(code=4401)
            return
        await self.channel_layer.group_add("attendance", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("attendance", self.channel_name)

    async def attendance_update(self, event):
        await self.send(text_data=json.dumps(event["data"]))
