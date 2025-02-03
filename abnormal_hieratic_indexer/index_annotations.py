# Index annotations stored in a Simple Annotation Server in Elasticsearch
# © Leiden University Libraries, 2019
# See the LICENSE file for licensing information.

import argparse
import requests
import json
from bs4 import BeautifulSoup, NavigableString
import logging
import schedule
import time


class Indexer:

    def __init__(self, config: argparse.Namespace):
        self.LOGGER = logging.getLogger(__file__)
        self.LOGGER.setLevel(logging.DEBUG)
        log_handler = logging.StreamHandler()
        log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        self.LOGGER.addHandler(log_handler)

        self.ES_ENDPOINT = config.target
        self.ES_INDEX = config.index
        self.SAS_ENDPOINT = config.source
        self.OA_ANNOTATIONS_URI = self.SAS_ENDPOINT + "/"
        self.SESSION = requests.Session()
        self.canvas_filter = config.canvas_uri_prefix

        self.ANNOTATED_BY = "dcterms:creator"
        self.ANNOTATOR_NAME = "foaf:name"

        self.manifest_labels = {}
        self.canvas_labels = {}
        self.canvas_images = {}
        # Links from Canvas URIs to WordPress pages
        self.canvas_pages = {}


    def parse_manifest(self, uri: str):
        self.LOGGER.info("Parsing manifest {}".format(uri))
        manifest = self.SESSION.get(uri).json()
        self.manifest_labels[uri] = manifest["label"]
        for seq in manifest["sequences"]:
            for canvas in seq["canvases"]:
                canvas_labels[canvas["@id"]] = canvas["label"]
                canvas_images[canvas["@id"]] = {"base": canvas["images"][0]["resource"]["service"]["@id"],
                                                "h": canvas["images"][0]["resource"]["height"],
                                                "w": canvas["images"][0]["resource"]["width"],
                                                "h_ratio": canvas["height"] / canvas["images"][0]["resource"]["height"],
                                                "v_ratio": canvas["width"] / canvas["images"][0]["resource"]["width"]}
                for related in canvas.get("related"):
                    if related["label"] == "Abnormal Hieratic Global Portal":
                        self.canvas_pages[canvas["@id"]] = related["@id"]


    def get_manifest_label(self, uri: str):
        if uri not in self.manifest_labels:
            self.parse_manifest(uri)
        return self.manifest_labels[uri]


    def find_annotations(self):
        annotation_list = self.SESSION.get(self.OA_ANNOTATIONS_URI).json()
        if self.canvas_filter is None or self.canvas_filter == "":
            self.LOGGER.debug("Not filtering annotations")
            annotations = annotation_list["resources"]
        else:
            self.LOGGER.debug("Filtering annotations")
            annotations = [x for x in annotation_list["resources"] if self.canvas_filter in x["on"][0]["full"]]
        self.LOGGER.info("Found {} annotations".format(len(annotations)))
        return annotations


    def svg_image(self, tag):
        """Return True when this is an SVG image"""
        return tag and tag.name == "img" and tag["src"].endswith(".svg")


    def convert_annotation(self, annotation):
        """Converts an annotation to flat JSON object for Elasticsearch"""
        record = {}
        # Find annotation ID
        # 'https://iiif.universiteitleiden.nl/anno/annotation/1540300675536' => 1540300675536
        record["id"] = annotation["@id"].replace(self.OA_ANNOTATIONS_URI, "")
        record["uri"] = annotation["@id"]
        chars = annotation["resource"][0]["chars"]
        soup = BeautifulSoup(chars, "html.parser")

        # Find SVG
        svg = soup.find_all(self.svg_image)
        if svg is not None:
            record["svg"] = [x["src"] for x in svg]

        # Find transliteration
        transliteration = soup.find("span",{"class": "transliteration"})
        if transliteration is not None:
            record["transliteration"] = transliteration.string

        # Find text type and translation
        for stripped_string in soup.stripped_strings:
            if stripped_string.startswith("Translation: "):
                record["translation"] = stripped_string.replace("Translation: ", "")
            elif stripped_string.startswith("Type: "):
                record["type"] = stripped_string.replace("Type: ", "")

        # Find annotator
        try:
            record["annotator"] = annotation[self.ANNOTATED_BY][self.ANNOTATOR_NAME]
        except KeyError:
            record["annotator"] = "Anonymous"

        # Find dates
        record["created"] = annotation["dcterms:created"]
        if "dcterms:modified" in annotation:
            record["modified"] = annotation["dcterms:modified"]

        # Find manifest, canvas with labels
        for target in annotation["on"]:
            if target["@type"] == "oa:SpecificResource" and "within" in target:
                if isinstance(target["within"], str):
                    record["manifest"] = target["within"]
                else:
                    record["manifest"] = target["within"]["@id"]
                record["manifest_label"] = self.get_manifest_label(record["manifest"])
                record["canvas"] = target["full"]
                record["canvas_label"] = self.canvas_labels[target["full"]]
                record["portal_url"] = self.canvas_pages[target["full"]]

                # Find coordinates; FIXME in case of non-choice
                record["xywh"] = target["selector"]["default"]["value"].replace("xywh=", "")
                record["x"], record["y"], record["w"], record["h"] = record["xywh"].split(",")

                # Find image URLs
                record["image_base_url"] = self.canvas_images[target["full"]]["base"]
                record["image_full_url"] = record["image_base_url"] + "/" + record["xywh"] + "/full/0/default.jpg"

        self.LOGGER.debug(json.dumps(record, indent=2))
        return record


    def index_annotation_record(self, record):
        self.LOGGER.info("Updating %s", record["id"])

        res = self.SESSION.put(self.ES_ENDPOINT + self.ES_INDEX + record["id"], json=record)
        self.LOGGER.info("Update returned status: %s", res.status_code)
        self.LOGGER.debug(res.json())
        res.raise_for_status()


    def run(self):
        annos = self.find_annotations()
        for anno in annos:
            rec = self.convert_annotation(anno)
            self.index_annotation_record(rec)
        self.LOGGER.info("Done indexing")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Base URL for the Simple Annotation Server")
    parser.add_argument("target", help="Base URL for ElasticSearch (default: %(default)s)", default="http://localhost:9200")
    parser.add_argument("--canvas-uri-prefix", help="Index only annotations targeting canvases whose URIs start with this prefix (default: %(default)s)",
    default="https://lab.library.universiteitleiden.nl/manifests/external/louvre/")
    parser.add_argument("--index", help="URI path to the ElasticSearch index (default: %(default)s)", default="/annotations/anno/")
    parser.add_argument("--schedule-hourly", help="Schedule to run this script hourly. "
                        "Because cron is not always easily available.",
                        action="store_true")
    args = parser.parse_args()
    indexer = Indexer(args)
    if args.schedule_hourly:
        schedule.every().hour.do(indexer.run)
        while True:
            schedule.run_pending()
            time.sleep(120)
    else:
        indexer.run()


if __name__ == "__main__":
    main()
