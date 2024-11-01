import os

os.environ["DO_NOT_TRACK"] = "true"
import asyncio
import pandas as pd
from tqdm import tqdm
from ragas.llms import LangchainLLMWrapper
from ragas.embeddings import LangchainEmbeddingsWrapper
from evalscope.backend.rag_eval import EmbeddingModel, LLM
from evalscope.backend.rag_eval.ragas.arguments import TestsetGenerationArguments
from evalscope.utils.logger import get_logger
from .translate_prompt import translate_prompts

logger = get_logger()


def get_transform(llm, embedding, language: None):
    """
    Creates and returns a default set of transforms for processing a knowledge graph.

    This function defines a series of transformation steps to be applied to a
    knowledge graph, including extracting summaries, keyphrases, titles,
    headlines, and embeddings, as well as building similarity relationships
    between nodes.

    The transforms are applied in the following order:
    1. Parallel extraction of summaries and headlines
    2. Embedding of summaries for document nodes
    3. Splitting of headlines
    4. Parallel extraction of embeddings, keyphrases, and titles
    5. Building cosine similarity relationships between nodes
    6. Building cosine similarity relationships between summaries

    Returns
    -------
    Transforms
        A list of transformation steps to be applied to the knowledge graph.

    """
    from ragas.testset.transforms.engine import Parallel
    from ragas.testset.transforms.extractors import (
        EmbeddingExtractor,
        HeadlinesExtractor,
        KeyphrasesExtractor,
        SummaryExtractor,
        TitleExtractor,
    )
    from ragas.testset.transforms.relationship_builders.cosine import (
        CosineSimilarityBuilder,
        SummaryCosineSimilarityBuilder,
    )
    from ragas.testset.transforms.splitters import HeadlineSplitter
    from ragas.testset.graph import NodeType

    # define the transforms
    summary_extractor = SummaryExtractor(llm=llm)
    keyphrase_extractor = KeyphrasesExtractor(llm=llm)
    title_extractor = TitleExtractor(llm=llm)
    headline_extractor = HeadlinesExtractor(llm=llm)

    asyncio.run(
        translate_prompts(
            prompts=[
                summary_extractor,
                keyphrase_extractor,
                title_extractor,
                headline_extractor,
            ],
            target_lang=language,
            llm=llm,
            adapt_instruction=True,
        )
    )

    embedding_extractor = EmbeddingExtractor(embedding_model=embedding)
    headline_splitter = HeadlineSplitter()
    cosine_sim_builder = CosineSimilarityBuilder(threshold=0.8)
    summary_embedder = EmbeddingExtractor(
        name="summary_embedder",
        filter_nodes=lambda node: True if node.type == NodeType.DOCUMENT else False,
        property_name="summary_embedding",
        embed_property_name="summary",
        embedding_model=embedding,
    )
    summary_cosine_sim_builder = SummaryCosineSimilarityBuilder(threshold=0.6)

    # specify the transforms and their order to be applied
    transforms = [
        Parallel(summary_extractor, headline_extractor),
        summary_embedder,
        headline_splitter,
        Parallel(embedding_extractor, keyphrase_extractor, title_extractor),
        cosine_sim_builder,
        summary_cosine_sim_builder,
    ]
    return transforms


def get_distribution(llm, distribution, language: None):
    from ragas.testset.synthesizers.abstract_query import (
        AbstractQuerySynthesizer,
        ComparativeAbstractQuerySynthesizer,
    )
    from ragas.testset.synthesizers.specific_query import SpecificQuerySynthesizer

    abstract = AbstractQuerySynthesizer(llm=llm)
    comparative = ComparativeAbstractQuerySynthesizer(llm=llm)
    specific = SpecificQuerySynthesizer(llm=llm)

    asyncio.run(
        translate_prompts(
            prompts=[
                abstract,
                comparative,
                specific,
            ],
            target_lang=language,
            llm=llm,
            adapt_instruction=True,
        )
    )
    return [
        (abstract, distribution["simple"]),
        (comparative, distribution["multi_context"]),
        (specific, distribution["reasoning"]),
    ]


def load_data(file_path):
    from langchain_community.document_loaders import UnstructuredFileLoader

    loader = UnstructuredFileLoader(file_path)
    data = loader.load()
    return data


def generate_testset(args: TestsetGenerationArguments) -> None:
    # from langchain_community.document_loaders

    from ragas.testset import TestsetGenerator
    from ragas import RunConfig

    # load data
    data = load_data(args.docs)

    # generator with models
    generator_llm = LLM.load(**args.generator_llm)
    embeddings = EmbeddingModel.load(**args.embeddings)

    # Change resulting question type distribution
    distributions = get_distribution(
        LangchainLLMWrapper(generator_llm), args.distribution, args.language
    )

    # get transforms
    transforms = get_transform(
        LangchainLLMWrapper(generator_llm),
        LangchainEmbeddingsWrapper(embeddings),
        args.language,
    )

    generator = TestsetGenerator.from_langchain(generator_llm)

    runconfig = RunConfig(
        timeout=600, max_retries=5, max_wait=120, max_workers=1, log_tenacity=True
    )
    testset = generator.generate_with_langchain_docs(
        documents=data,
        testset_size=args.test_size,
        transforms=transforms,
        query_distribution=distributions,
        run_config=runconfig,
        with_debugging_logs=True,
        raise_exceptions=True,
    )

    # save file
    testset_df = testset.to_pandas()
    output_path = os.path.dirname(args.output_file)
    os.makedirs(output_path, exist_ok=True)
    testset_df.to_json(
        args.output_file, indent=4, index=False, orient="records", force_ascii=False
    )

    # get answer
    testset_with_answer = get_answer(testset_df, generator_llm, args.language)
    testset_with_answer.to_json(
        args.output_file.replace(".json", "_with_answer.json"),
        indent=4,
        index=False,
        orient="records",
        force_ascii=False,
    )


def get_answer(testset_df, generator_llm, language: None):
    template = """You are an assistant for question-answering tasks. 
Use the following pieces of retrieved context to answer the question. 
If you don't know the answer, just say that you don't know. 
Use two sentences maximum and keep the answer concise. Answer in {language}.
Question: {question} 
Context: {contexts} 
Answer:
"""

    items = []
    for i in tqdm(range(len(testset_df)), desc="Generating Answers"):
        row = testset_df.iloc[i]
        question = row["user_input"]
        contexts = "\n".join(row["reference_contexts"])

        # Combine question and contexts as input for the LLM
        input_text = template.format(
            language=language, question=question, contexts=contexts
        )

        # Generate the answer using the generator LLM
        answer = generator_llm.invoke(input_text).content
        items.append(
            {
                "user_input": question,
                "retrieved_contexts": row["reference_contexts"],
                "response": answer,
                "reference": row["reference"],
            }
        )

    return pd.DataFrame.from_dict(items)
