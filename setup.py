import setuptools
import glob
import os


DEPENDENCIES = [
    "ujson",
    "NewlineJSON",
]


data_files = [os.path.basename(x)
              for x in glob.glob("classification/data/*.csv")]

setuptools.setup(
    name='vessel_inference',
    version='0.1.3',
    author='Tim Hochberg',
    author_email='tim@globalfishingwatch.com',
    package_data={
        'classification.data': data_files
    },
    packages=[
        'common',
        'classification',
        'classification.data',
        'classification.models',
        'classification.models.prod',
        'classification.models.dev',
    ],
    install_requires=DEPENDENCIES  #+ DATAFLOW_PINNED_DEPENDENCIES
)