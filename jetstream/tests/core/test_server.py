# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests gRPC server end-to-end.

See orchestrator test for why these characters specifically will be the
response.
"""

from typing import Any, Type
import unittest


import requests
from parameterized import parameterized
import grpc
from jetstream.core import config_lib
from jetstream.core import server_lib
from jetstream.core.proto import jetstream_pb2
from jetstream.core.proto import jetstream_pb2_grpc
from jetstream.engine import engine_api
import portpicker


class ServerTest(unittest.IsolatedAsyncioTestCase):

  @parameterized.expand(
      [
          # Uses weight 2 for prefill, 4 for decode.
          (
              config_lib.CPUTestServer,
              True,
              ["Ċ", "Ō", "Ɵ", ""],
              [266, 332, 415, None],
              [None, None],
          ),
          # Uses the same prefill / generate weights (2).
          (
              config_lib.InterleavedCPUTestServer,
              True,
              ["Ċ", "Ə", "ɖ", ""],
              [266, 399, 598, None],
              [None],
          ),
          # Disable the metrics server.
          (
              config_lib.InterleavedCPUTestServer,
              False,
              ["Ċ", "Ə", "ɖ", ""],
              [266, 399, 598, None],
              [None],
          ),
      ]
  )
  async def test_server(
      self,
      config: Type[config_lib.ServerConfig],
      metrics_enabled: bool,
      expected_text: list[str],
      expected_token_ids: list[int | None],
      devices: list[Any],
  ):
    """Sets up a server and requests token responses."""
    ######################### Server side ######################################
    port = portpicker.pick_unused_port()
    metrics_port = portpicker.pick_unused_port()

    print("port: " + str(port))
    credentials = grpc.local_server_credentials()

    server = server_lib.run(
        port=port,
        config=config,
        devices=devices,
        credentials=credentials,
        metrics_server_config=config_lib.MetricsServerConfig(
            port=metrics_port, model_name="some_model_name"
        )
        if metrics_enabled is True
        else None,
    )
    ###################### Requester side ######################################

    # if prometheus not configured, assert no metrics collector on Driver
    if metrics_enabled is not True:
      assert server._driver._metrics_collector is None  # pylint: disable=protected-access

    async with grpc.aio.secure_channel(
        f"localhost:{port}", grpc.local_channel_credentials()
    ) as channel:
      stub = jetstream_pb2_grpc.OrchestratorStub(channel)

      healthcheck_request = jetstream_pb2.HealthCheckRequest()
      healthcheck_response = stub.HealthCheck(healthcheck_request)
      healthcheck_response = await healthcheck_response

      assert healthcheck_response.is_live is True

      # The string representation of np.array([[65, 66]]), [2] will be prepended
      # as BOS
      text = "AB"
      request = jetstream_pb2.DecodeRequest(
          text_content=jetstream_pb2.DecodeRequest.TextContent(text=text),
          max_tokens=3,
      )
      iterator = stub.Decode(request)
      counter = 0
      async for resp in iterator:
        output_text = resp.stream_content.samples[0].text
        token_ids = resp.stream_content.samples[0].token_ids
        output_token_id = token_ids[0] if len(token_ids) > 0 else None
        print(f"actual output: {output_text=} {output_token_id=}")
        assert output_text == expected_text[counter]
        assert output_token_id == expected_token_ids[counter]
        counter += 1
      # assert prometheus server is running and responding
      if metrics_enabled is True:
        assert server._driver._metrics_collector is not None  # pylint: disable=protected-access
        assert (
            requests.get(
                f"http://localhost:{metrics_port}", timeout=5
            ).status_code
            == requests.status_codes.codes["ok"]
        )
      server.stop()

  def test_jax_profiler_server(self):
    port = portpicker.pick_unused_port()
    print("port: " + str(port))
    credentials = grpc.local_server_credentials()
    # Now test server with prometheus config
    server = server_lib.run(
        port=port,
        config=config_lib.InterleavedCPUTestServer,
        devices=[None],
        credentials=credentials,
        enable_jax_profiler=True,
    )
    assert server
    server.stop()

  def test_get_devices(self):
    assert len(server_lib.get_devices()) == 1

  async def test_model_warmup(self):
    port = portpicker.pick_unused_port()

    print("port: " + str(port))
    credentials = grpc.local_server_credentials()

    server = server_lib.run(
        port=port,
        config=config_lib.InterleavedCPUTestServer,
        devices=[None],
        credentials=credentials,
        enable_model_warmup=True,
    )

    async with grpc.aio.secure_channel(
        f"localhost:{port}", grpc.local_channel_credentials()
    ) as channel:
      stub = jetstream_pb2_grpc.OrchestratorStub(channel)

      healthcheck_request = jetstream_pb2.HealthCheckRequest()
      healthcheck_response = stub.HealthCheck(healthcheck_request)
      healthcheck_response = await healthcheck_response

      assert healthcheck_response.is_live is True

      for pe in server._driver._prefill_engines:  # pylint: disable=protected-access
        assert isinstance(pe, engine_api.JetStreamEngine)
        assert pe.warm is True

      for ge in server._driver._generate_engines:  # pylint: disable=protected-access
        assert isinstance(ge, engine_api.JetStreamEngine)
        assert ge.warm is True

      server.stop()
