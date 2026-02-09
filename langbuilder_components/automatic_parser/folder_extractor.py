from langbuilder.custom import Component
from langbuilder.io import DataInput, Output
from langbuilder.schema.message import Message


class FolderIdExtractor(Component):
    display_name = "Folder ID Extractor"
    description = "Extracts folder_id from PM Config Reader output."
    icon = "filter"
    name = "FolderIdExtractor"

    inputs = [
        DataInput(
            name="pm_data",
            display_name="PM Data",
            required=True,
        ),
    ]

    outputs = [
        Output(
            name="folder_id",
            display_name="Folder ID",
            method="extract",
        ),
    ]

    def extract(self) -> Message:
        data = self.pm_data
        if hasattr(data, "data"):
            data = data.data

        configs = data.get("pm_configs", [])
        folder_id = configs[0]["gdrive_config"]["folder_id"] if configs else ""

        self.status = folder_id
        return Message(text=folder_id)