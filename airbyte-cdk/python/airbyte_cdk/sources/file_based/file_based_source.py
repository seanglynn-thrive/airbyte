#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import logging
import traceback
from abc import ABC
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type

from airbyte_cdk.models import ConfiguredAirbyteCatalog
from airbyte_cdk.models.airbyte_protocol import ConnectorSpecification
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.file_based.config.abstract_file_based_spec import AbstractFileBasedSpec
from airbyte_cdk.sources.file_based.config.file_based_stream_config import FileBasedStreamConfig
from airbyte_cdk.sources.file_based.default_file_based_availability_strategy import DefaultFileBasedAvailabilityStrategy
from airbyte_cdk.sources.file_based.discovery_policy import AbstractDiscoveryPolicy, DefaultDiscoveryPolicy
from airbyte_cdk.sources.file_based.exceptions import ConfigValidationError, FileBasedSourceError
from airbyte_cdk.sources.file_based.file_based_stream_reader import AbstractFileBasedStreamReader
from airbyte_cdk.sources.file_based.file_types import default_parsers
from airbyte_cdk.sources.file_based.file_types.file_type_parser import FileTypeParser
from airbyte_cdk.sources.file_based.schema_validation_policies import DEFAULT_SCHEMA_VALIDATION_POLICIES, AbstractSchemaValidationPolicy
from airbyte_cdk.sources.file_based.stream import AbstractFileBasedStream, DefaultFileBasedStream
from airbyte_cdk.sources.file_based.stream.cursor.default_file_based_cursor import DefaultFileBasedCursor
from airbyte_cdk.sources.streams.availability_strategy import AvailabilityStrategy
from pydantic.error_wrappers import ValidationError

DEFAULT_MAX_HISTORY_SIZE = 10_000


class FileBasedSource(AbstractSource, ABC):
    def __init__(
        self,
        stream_reader: AbstractFileBasedStreamReader,
        catalog: Optional[ConfiguredAirbyteCatalog],
        availability_strategy: Optional[AvailabilityStrategy],
        spec_class: Type[AbstractFileBasedSpec],
        discovery_policy: AbstractDiscoveryPolicy = DefaultDiscoveryPolicy(),
        parsers: Dict[str, FileTypeParser] = None,
        validation_policies: Dict[str, AbstractSchemaValidationPolicy] = DEFAULT_SCHEMA_VALIDATION_POLICIES,
        max_history_size: int = DEFAULT_MAX_HISTORY_SIZE,
    ):
        self.stream_reader = stream_reader
        self.availability_strategy = availability_strategy or DefaultFileBasedAvailabilityStrategy(stream_reader)
        self.spec_class = spec_class
        self.discovery_policy = discovery_policy
        self.parsers = parsers or default_parsers
        self.validation_policies = validation_policies
        self.stream_schemas = {s.stream.name: s.stream.json_schema for s in catalog.streams} if catalog else {}
        self.max_history_size = max_history_size

    def check_connection(self, logger: logging.Logger, config: Mapping[str, Any]) -> Tuple[bool, Optional[Any]]:
        """
        Check that the source can be accessed using the user-provided configuration.

        For each stream, verify that we can list and read files.

        Returns (True, None) if the connection check is successful.

        Otherwise, the "error" object should describe what went wrong.
        """
        streams = self.streams(config)
        if len(streams) == 0:
            return (
                False,
                f"No streams are available for source {self.name}. This is probably an issue with the connector. Please verify that your "
                f"configuration provides permissions to list and read files from the source. Contact support if you are unable to "
                f"resolve this issue.",
            )

        errors = []
        for stream in streams:
            try:
                (
                    stream_is_available,
                    reason,
                ) = stream.availability_strategy.check_availability(stream, logger, self)
            except Exception:
                errors.append(f"Unable to connect to stream {stream} - {''.join(traceback.format_exc())}")
            else:
                if not stream_is_available:
                    errors.append(reason)

        return not bool(errors), (errors or None)

    def _validate_stream_config(self, stream_config: FileBasedStreamConfig):
        if stream_config.validation_policy not in self.validation_policies:
            raise ValidationError(
                f"`validation_policy` must be one of {list(self.validation_policies.keys())}", model=FileBasedStreamConfig
            )
        if stream_config.input_schema and stream_config.schemaless:
            raise ValidationError("`input_schema` and `schemaless` options cannot both be set", model=FileBasedStreamConfig)

    def streams(self, config: Mapping[str, Any]) -> List[AbstractFileBasedStream]:
        """
        Return a list of this source's streams.
        """
        try:
            parsed_config = self.spec_class(**config)
            streams = []
            for stream_config in parsed_config.streams:
                self._validate_stream_config(stream_config)
                streams.append(
                    DefaultFileBasedStream(
                        config=stream_config,
                        catalog_schema=self.stream_schemas.get(stream_config.name),
                        stream_reader=self.stream_reader,
                        availability_strategy=self.availability_strategy,
                        discovery_policy=self.discovery_policy,
                        parsers=self.parsers,
                        validation_policies=self.validation_policies,
                        cursor=DefaultFileBasedCursor(self.max_history_size, stream_config.days_to_sync_if_history_is_full),
                    )
                )
            return streams

        except ValidationError as exc:
            raise ConfigValidationError(FileBasedSourceError.CONFIG_VALIDATION_ERROR) from exc

    def spec(self, *args: Any, **kwargs: Any) -> ConnectorSpecification:
        """
        Returns the specification describing what fields can be configured by a user when setting up a file-based source.
        """

        return ConnectorSpecification(
            documentationUrl=self.spec_class.documentation_url(),
            connectionSpecification=self.spec_class.schema(),
        )
