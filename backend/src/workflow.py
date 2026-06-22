import json
from typing import TypedDict, List
from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage
from langchain_core.language_models.chat_models import BaseChatModel
from .prompt import classification_prompt, system_prompt, generation_template, contextualize_q_system_prompt

class AgentState(TypedDict):
    messages: List[BaseMessage]
    query: str
    contextualized_query: str
    intent: str
    risk_level: str
    db_context: str
    medicine_context: str # New field for Davis Drug Guide
    web_context: str
    patient_context: str
    doctor_context: str
    final_answer: str
    confidence: str

def create_workflow(llm: BaseChatModel, tools: list):
    clinical_tool = tools[0]
    medicine_tool = tools[1]
    web_tool = tools[2]

    # --- NODES ---

    def contextualize_node(state: AgentState):
        """Rewrites the user query to be standalone if there's history."""
        if not state["messages"]:
            return {"contextualized_query": state["query"]}
        
        messages = [
            ("system", contextualize_q_system_prompt)
        ] + state["messages"] + [
            ("human", state["query"])
        ]
        
        response = llm.invoke(messages)
        return {"contextualized_query": response.content.strip()}

    def classifier_node(state: AgentState):
        query = state["contextualized_query"]
        messages = [
            ("system", classification_prompt)
        ] + [
            ("human", f"Doctor specialty: {state['doctor_context']}\nQuery: {query}")
        ]
        response = llm.invoke(messages)
        try:
            clean_content = response.content.strip().replace("```json", "").replace("```", "")
            data = json.loads(clean_content)
        except:
            data = {"intent": "MEDICAL_CONCERN", "risk_level": "LOW"}
        
        return {
            "intent": data.get("intent", "MEDICAL_CONCERN"),
            "risk_level": data.get("risk_level", "LOW")
        }

    def clinical_retriever_node(state: AgentState):
        # Skip retrieval for simple greetings
        if state["intent"] == "GREETING":
            return {"db_context": "N/A"}
        
        # In a GP clinic, we use the contextualized query directly for broad retrieval
        enhanced_query = state["contextualized_query"]
        
        results = clinical_tool.invoke(enhanced_query)
        return {"db_context": str(results)}

    def medicine_retriever_node(state: AgentState):
        # STRICLY only retrieive medicine info if explicitly requested
        if state["intent"] != "MEDICINE_REQUEST":
            return {"medicine_context": "N/A"}
            
        query = state["contextualized_query"]
        results = medicine_tool.invoke(query)
        return {"medicine_context": str(results)}

    def web_search_node(state: AgentState):
        if state["intent"] == "GREETING":
            return {"web_context": "N/A"}
            
        query = state["contextualized_query"]
        results = web_tool.invoke(query)
        return {"web_context": str(results)}

    def generator_node(state: AgentState):
        # Final prompt combines all contexts
        # The prompt.py template will handle the persona logic
        from .prompt import system_prompt, generation_template
        
        # --- CONTEXT PURGING LOGIC ---
        # If the intent is a GREETING, we ZERO BREECH the clinical data.
        # This prevents the model from even seeing patient data for social chat.
        if state["intent"] == "GREETING":
            db_context = "N/A (Ignored for Greeting)"
            medicine_context = "N/A (Ignored for Greeting)"
            web_context = "N/A (Ignored for Greeting)"
            patient_context = "N/A (Ignored for Greeting)"
        else:
            db_context = state["db_context"]
            medicine_context = state.get("medicine_context", "N/A")
            web_context = state["web_context"]
            patient_context = state["patient_context"]

        prompt = generation_template.format(
            doctor_context=state["doctor_context"],
            patient_context=patient_context,
            intent=state["intent"],
            risk_level=state["risk_level"],
            db_context=db_context,
            medicine_context=medicine_context,
            web_context=web_context,
            query=state["query"]
        )
        
        messages = [
            ("system", system_prompt)
        ] + state["messages"] + [
            ("human", prompt)
        ]
        
        response = llm.invoke(messages)
        return {"final_answer": response.content}

    # --- GRAPH ---

    workflow = StateGraph(AgentState)

    workflow.add_node("contextualize", contextualize_node)
    workflow.add_node("classifier", classifier_node)
    workflow.add_node("clinical_retriever", clinical_retriever_node)
    workflow.add_node("medicine_retriever", medicine_retriever_node)
    workflow.add_node("web_search", web_search_node)
    workflow.add_node("generator", generator_node)

    workflow.set_entry_point("contextualize")
    
    workflow.add_edge("contextualize", "classifier")
    workflow.add_edge("classifier", "clinical_retriever")
    workflow.add_edge("clinical_retriever", "medicine_retriever")
    workflow.add_edge("medicine_retriever", "web_search")
    workflow.add_edge("web_search", "generator")
    workflow.add_edge("generator", END)

    return workflow.compile()

